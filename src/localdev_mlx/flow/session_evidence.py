"""Bounded, inert evidence imports. Source manifests are data, never instructions."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import tempfile
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath

from .evidence import digest_manifest
from .organization import validate_advice
from .workspace import atomic_write, files

MAX_FILES = 1024
MAX_BYTES = 110 * 1024 * 1024
MAX_FILE = 100 * 1024 * 1024
MAX_MANIFEST = 4 * 1024 * 1024
ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
SHA = re.compile(r"[a-f0-9]{64}\Z")


def open_regular(path):
    """Open each directory by descriptor: no parent-symlink swap between check/read."""
    path = Path(path).absolute()
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise ValueError("Evidence must be a regular file")
        return os.fdopen(descriptor, "rb")
    finally:
        os.close(directory)


def relative(value):
    if not isinstance(value, str) or "\\" in value or "\x00" in value:
        raise ValueError("Invalid evidence path")
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or value != path.as_posix()
        or any(part in {".", ".."} or ":" in part for part in value.split("/"))
    ):
        raise ValueError("Evidence paths must be safe, canonical relative paths")
    return value


def no_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON field in evidence")
        value[key] = item
    return value


class Bundle:
    def __init__(self, source):
        self.source = Path(source)
        if self.source.is_symlink() or any(p.is_symlink() for p in self.source.parents):
            raise ValueError("Symlinked evidence paths are not accepted")
        self.archive = None
        self.archive_stream = None
        self.index = {}
        self.normalized_names = set()
        try:
            if self.source.is_dir():
                for count, path in enumerate(self.source.rglob("*"), 1):
                    if count > MAX_FILES * 3:
                        raise ValueError("Evidence exceeds directory entry budget")
                    if path.is_symlink():
                        raise ValueError("Symlink in evidence bundle")
                    if path.is_dir():
                        continue
                    info = path.stat()
                    if not stat.S_ISREG(info.st_mode):
                        raise ValueError("Non-regular evidence file")
                    self.add(path.relative_to(self.source).as_posix(), info.st_size)
            else:
                if self.source.stat().st_size > MAX_BYTES:
                    raise ValueError("Evidence ZIP exceeds byte budget")
                self.archive_stream = open_regular(self.source)
                self.archive = zipfile.ZipFile(self.archive_stream)
                for info in self.archive.infolist():
                    mode = info.external_attr >> 16
                    if (
                        info.is_dir()
                        or (stat.S_IFMT(mode) not in {0, stat.S_IFREG})
                        or info.flag_bits & 1
                    ):
                        raise ValueError("Non-regular, encrypted or symlink ZIP entry")
                    self.add(info.filename, info.file_size)
        except BaseException:
            self.close()
            raise

    def add(self, name, size):
        relative(name)
        normalized = unicodedata.normalize("NFC", name).casefold()
        if normalized in self.normalized_names:
            raise ValueError("Duplicate ZIP entry")
        self.normalized_names.add(normalized)
        self.index[name] = size
        if size > MAX_FILE or len(self.index) > MAX_FILES or sum(self.index.values()) > MAX_BYTES:
            raise ValueError("Evidence exceeds file/count/expanded-byte budget")

    def read(self, name, limit=MAX_FILE):
        relative(name)
        if name not in self.index or self.index[name] > limit:
            raise ValueError("Missing or oversized evidence file")
        if self.archive:
            with self.archive.open(name) as stream:
                raw = stream.read(limit + 1)
        else:
            path = self.source / name
            if any(p.is_symlink() for p in [path, *path.parents]):
                raise ValueError("Evidence path changed to a symlink")
            with open_regular(path) as stream:
                raw = stream.read(limit + 1)
        if len(raw) > limit or len(raw) != self.index[name]:
            raise ValueError("Evidence changed or exceeds its byte budget")
        return raw

    def close(self):
        if self.archive:
            self.archive.close()
        if self.archive_stream:
            self.archive_stream.close()


def validate_bundle(bundle, project):
    manifest = json.loads(
        bundle.read("manifest.json", MAX_MANIFEST), object_pairs_hook=no_duplicates
    )
    if not isinstance(manifest, dict) or manifest.get("schema") != "project-flow.evidence/v1":
        raise ValueError(
            "Unsupported evidence schema; preserve the bundle without reinterpreting it"
        )
    if manifest.get("project") != str(project.resolve()):
        raise ValueError("Evidence belongs to a different project")
    issues, selections = manifest.get("issues"), manifest.get("selections")
    if (
        not isinstance(issues, list)
        or not 1 <= len(issues) <= 60
        or not isinstance(selections, dict)
        or len(selections) > 100
    ):
        raise ValueError("Invalid issue or capture count")
    ids = []
    for row in issues:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("id"), str)
            or not ID.fullmatch(row["id"])
        ):
            raise ValueError("Invalid issue ID")
        ids.append(row["id"])
        if (
            not isinstance(row.get("prompt"), str)
            or not row["prompt"].strip()
            or len(row["prompt"]) > 12000
        ):
            raise ValueError("Missing or oversized original description")
        if not isinstance(row.get("expected", ""), str) or len(row.get("expected", "")) > 4000:
            raise ValueError("Invalid expected-result description")
        refs = row.get("selection_ids", [])
        if (
            not isinstance(refs, list)
            or any(not isinstance(s, str) or s not in selections for s in refs)
            or len(set(refs)) != len(refs)
        ):
            raise ValueError("Invalid or duplicate capture references")
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate issue ID")
    order = manifest.get("suggested_order", ids)
    if not isinstance(order, list) or len(order) != len(ids) or set(order) != set(ids):
        raise ValueError("Invalid suggested issue order")
    if manifest.get("advice"):
        checked = validate_advice({"issues": list(manifest["advice"].values())}, issues)
        if set(checked) != set(manifest["advice"]):
            raise ValueError("Advice keys do not match stable issue IDs")
    revision = manifest.get("issues_revision", 0)
    if type(revision) is not int or revision < 0:
        raise ValueError("Invalid issue revision")
    expected = {}
    for sid, selection in selections.items():
        if not ID.fullmatch(sid) or not isinstance(selection, dict) or selection.get("id") != sid:
            raise ValueError("Invalid stable capture ID")
        artifacts, hashes = selection.get("artifacts", {}), selection.get("artifact_sha256", {})
        if (
            not isinstance(artifacts, dict)
            or not isinstance(hashes, dict)
            or set(artifacts) != set(hashes)
        ):
            raise ValueError("Artifact hash index does not match artifacts")
        for name, path in artifacts.items():
            relative(path)
            if (
                not path.startswith("evidence/")
                or not isinstance(hashes[name], str)
                or not SHA.fullmatch(hashes[name])
            ):
                raise ValueError("Invalid artifact path or SHA-256")
            if path in expected and expected[path] != hashes[name]:
                raise ValueError("Conflicting shared artifact hashes")
            expected[path] = hashes[name]
    omissions = manifest.get("omitted_artifacts", [])
    if not isinstance(omissions, list) or len(omissions) > MAX_FILES:
        raise ValueError("Invalid artifact omissions")
    for item in omissions:
        if (
            not isinstance(item, dict)
            or item.get("selection_id") not in selections
            or not isinstance(item.get("reason"), str)
            or not item["reason"].strip()
        ):
            raise ValueError("Missing optional-artifact omission reason")
        if item.get("artifact") in selections[item["selection_id"]].get("artifacts", {}):
            raise ValueError("An omitted artifact is incorrectly represented as included")
    if set(bundle.index) != {"manifest.json", "handoff.md", *expected}:
        raise ValueError("Missing or unindexed evidence files; optional omissions must be explicit")
    file_hashes = {}
    for name in bundle.index:
        limit = MAX_MANIFEST if name in {"manifest.json", "handoff.md"} else MAX_FILE
        digest = hashlib.sha256(bundle.read(name, limit)).hexdigest()
        if name in expected and digest != expected[name]:
            raise ValueError("Artifact hash mismatch; evidence was modified")
        file_hashes[name] = digest
    if manifest.get("handoff_sha256") and manifest["handoff_sha256"] != file_hashes["handoff.md"]:
        raise ValueError("Handoff hash mismatch")
    if manifest.get("artifact_bytes", sum(bundle.index[p] for p in expected)) != sum(
        bundle.index[p] for p in expected
    ):
        raise ValueError("Artifact byte count mismatch")
    content = copy.deepcopy(manifest)
    # Render paths, performance/caching observations are not new user requests.
    content.pop("handoff_sha256", None)
    for key in ("cached", "duration_seconds", "usage", "loaded_by_helper"):
        content.get("organization", {}).pop(key, None)
    digest = hashlib.sha256(
        json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return manifest, digest, file_hashes


def link_export(project, source):
    from localdev_mlx.sessions import SessionBusy, SessionStore

    project = Path(project).resolve()
    if not (project / ".localdev/config.toml").is_file():
        return {
            "state": "not_linked",
            "message": "Not linked to a LocalDev session: project is not initialized. Export is complete.",
            "retryable": True,
        }
    bundle = None
    try:
        # Validate first, then copy/hash before acquiring the short session operation lock.
        bundle = Bundle(source)
        manifest, digest, hashes = validate_bundle(bundle, project)
        store = SessionStore(project)
        if store.repository != project:
            raise ValueError("Flow project must match the LocalDev session project exactly")
        evidence_root = store.root / "evidence"
        if evidence_root.is_symlink():
            raise ValueError("Session evidence must not be a symlink")
        evidence_root.mkdir(mode=0o700, exist_ok=True)
        target = evidence_root / digest
        existing_verified = False
        if target.exists():
            old = Bundle(target / "evidence.zip")
            try:
                old_manifest, old_digest, old_hashes = validate_bundle(old, project)
                if old_digest != digest:
                    raise ValueError("Historical evidence content digest changed")
                for name, expected in old_hashes.items():
                    path = target / name
                    if (
                        path.is_symlink()
                        or any(p.is_symlink() for p in path.parents)
                        or not path.is_file()
                        or path.stat().st_size > MAX_FILE
                    ):
                        raise ValueError("Historical evidence is missing or changed")
                    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                        raise ValueError("Historical evidence hash mismatch")
                existing_verified = True
            finally:
                old.close()
        warnings = [
            f"{item['selection_id']}: {item['reason']}"
            for item in manifest.get("omitted_artifacts", [])
        ]
        from .controller import config_for

        current = digest_manifest(files(config_for(project), project))
        recorded = manifest.get("inspection", {}).get("fingerprint")
        if recorded and recorded != current:
            warnings.append(
                "Source has changed since preview capture; inspect current code before implementation"
            )
        with tempfile.TemporaryDirectory(prefix=".incoming-", dir=evidence_root) as temporary:
            stage = Path(temporary) / "bundle"
            stage.mkdir(mode=0o700)
            for name, expected in hashes.items():
                raw = bundle.read(name)
                if hashlib.sha256(raw).hexdigest() != expected:
                    raise ValueError("Evidence changed during import")
                atomic_write(stage / name, raw)
            # Rebuild a safe, deterministic file set; never extract or execute a ZIP entry.
            with zipfile.ZipFile(
                stage / "evidence.zip", "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                for name in sorted(hashes):
                    archive.write(stage / name, name)
            entry = {
                "project": str(project),
                "bundle_digest": digest,
                "schema": manifest["schema"],
                "issues_revision": manifest.get("issues_revision", 0),
                "manifest": manifest,
                "artifact_index": hashes,
                "warnings": warnings,
                "markdown": str(target / "handoff.md"),
                "zip": str(target / "evidence.zip"),
            }
            with store._lock():
                if target.is_symlink():
                    raise ValueError("Session evidence target is a symlink")
                if not target.exists():
                    stage.rename(target)
                else:
                    if not existing_verified:
                        raise ValueError("Another export just stored this evidence; retry linking")
                    entry["manifest"] = old_manifest
                    entry["artifact_index"] = old_hashes
                session, duplicate = store.record_flow_evidence(entry)
        return {
            "state": "linked",
            "message": f"Linked to Session {session['number']:04d}"
            + (" (already recorded)" if duplicate else ""),
            "session": session["number"],
            "handoff": str(store.handoff(session)),
            "digest": digest,
            "retryable": False,
            "warnings": warnings,
        }
    except SessionBusy:
        return {
            "state": "not_linked",
            "message": "Session busy. Export is complete; retry the session link when the local task finishes.",
            "retryable": True,
        }
    except Exception as exc:
        return {
            "state": "not_linked",
            "message": f"Session link failed ({type(exc).__name__}): {str(exc)[:200]}. Export is preserved.",
            "retryable": True,
        }
    finally:
        if bundle:
            bundle.close()
