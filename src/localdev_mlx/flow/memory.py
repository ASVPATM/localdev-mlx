from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

INDEXED_SUFFIXES = {
    ".css",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
SKIPPED_NAMES = {"uv.lock"}


@dataclass(frozen=True)
class MemoryStats:
    documents: int
    chunks: int
    estimated_tokens: int


class ContextStore:
    """Durable project memory with lexical retrieval.

    The database may grow well beyond any individual model's context window. Calls
    receive only the highest-ranking chunks that fit their configured retrieval budget.
    """

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._fts_enabled = True
        self._initialize()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY,
                document_key TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,
                source TEXT NOT NULL,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY,
                document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                content TEXT NOT NULL,
                UNIQUE(document_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY,
                run_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        try:
            self.connection.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    content,
                    source UNINDEXED,
                    kind UNINDEXED,
                    document_id UNINDEXED,
                    tokenize='porter unicode61'
                )
                """
            )
        except sqlite3.OperationalError:
            self._fts_enabled = False
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    @staticmethod
    def _chunks(content: str, *, size: int = 6_000, overlap: int = 500) -> list[str]:
        if not content:
            return []
        chunks: list[str] = []
        start = 0
        while start < len(content):
            end = min(len(content), start + size)
            if end < len(content):
                boundary = content.rfind("\n", start + size // 2, end)
                if boundary > start:
                    end = boundary
            chunks.append(content[start:end])
            if end >= len(content):
                break
            start = max(start + 1, end - overlap)
        return chunks

    def put_document(self, key: str, kind: str, source: str, content: str) -> bool:
        digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
        existing = self.connection.execute(
            "SELECT id, content_hash FROM documents WHERE document_key = ?", (key,)
        ).fetchone()
        if existing and existing["content_hash"] == digest:
            return False

        now = datetime.now(UTC).isoformat()
        if existing:
            document_id = int(existing["id"])
            self.connection.execute(
                """
                UPDATE documents
                SET kind = ?, source = ?, content = ?, content_hash = ?, updated_at = ?
                WHERE id = ?
                """,
                (kind, source, content, digest, now, document_id),
            )
            self._delete_chunks(document_id)
        else:
            cursor = self.connection.execute(
                """
                INSERT INTO documents(document_key, kind, source, content, content_hash, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (key, kind, source, content, digest, now),
            )
            document_id = int(cursor.lastrowid)

        for ordinal, chunk in enumerate(self._chunks(content)):
            self.connection.execute(
                "INSERT INTO chunks(document_id, ordinal, content) VALUES (?, ?, ?)",
                (document_id, ordinal, chunk),
            )
            if self._fts_enabled:
                self.connection.execute(
                    """
                    INSERT INTO chunks_fts(content, source, kind, document_id)
                    VALUES (?, ?, ?, ?)
                    """,
                    (chunk, source, kind, document_id),
                )
        self.connection.commit()
        return True

    def _delete_chunks(self, document_id: int) -> None:
        self.connection.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
        if self._fts_enabled:
            self.connection.execute("DELETE FROM chunks_fts WHERE document_id = ?", (document_id,))

    def add_event(self, run_id: str, kind: str, content: str) -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            "INSERT INTO events(run_id, kind, content, created_at) VALUES (?, ?, ?, ?)",
            (run_id, kind, content, now),
        )
        self.connection.commit()
        self.put_document(f"run:{run_id}:{kind}:{now}", "run", f"run/{run_id}/{kind}", content)

    def index_files(self, repo: Path, paths: list[str]) -> int:
        """Index only the caller's explicitly safe snapshot file set."""
        seen = set()
        indexed = 0
        for relative in paths:
            path = repo / relative
            if path.suffix.casefold() not in INDEXED_SUFFIXES:
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            key = f"repo:{relative}"
            seen.add(key)
            indexed += self.put_document(key, "repository", relative, content)
        for row in self.connection.execute(
            "SELECT id, document_key FROM documents WHERE kind = 'repository'"
        ).fetchall():
            if row["document_key"] not in seen:
                self._delete_chunks(int(row["id"]))
                self.connection.execute("DELETE FROM documents WHERE id = ?", (row["id"],))
        self.connection.commit()
        return indexed

    @staticmethod
    def _query_terms(query: str) -> list[str]:
        terms: list[str] = []
        for term in re.findall(r"[A-Za-z0-9_./-]+", query.casefold()):
            if len(term) < 2 or term in terms:
                continue
            terms.append(term)
        return terms[:32]

    def retrieve(self, query: str, *, token_budget: int) -> str:
        if token_budget <= 0:
            return ""
        rows: list[sqlite3.Row]
        terms = self._query_terms(query)
        if self._fts_enabled and terms:
            expression = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)
            try:
                rows = self.connection.execute(
                    """
                    SELECT source, kind, content, bm25(chunks_fts) AS score
                    FROM chunks_fts
                    WHERE chunks_fts MATCH ?
                    ORDER BY score
                    LIMIT 200
                    """,
                    (expression,),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
        else:
            rows = []
        if not rows:
            rows = self.connection.execute(
                """
                SELECT documents.source, documents.kind, chunks.content, chunks.id AS score
                FROM chunks
                JOIN documents ON documents.id = chunks.document_id
                ORDER BY documents.updated_at DESC, chunks.id DESC
                LIMIT 200
                """
            ).fetchall()

        max_chars = token_budget * 4
        selected: list[str] = []
        used = 0
        for row in rows:
            block = f"\n--- {row['kind']}: {row['source']} ---\n{row['content'].strip()}\n"
            if used + len(block) > max_chars:
                remaining = max_chars - used
                if remaining > 800:
                    selected.append(block[:remaining])
                break
            selected.append(block)
            used += len(block)
        return "".join(selected)

    def stats(self) -> MemoryStats:
        row = self.connection.execute(
            """
            SELECT
                COUNT(*) AS documents,
                COALESCE(SUM(LENGTH(content)), 0) AS characters
            FROM documents
            """
        ).fetchone()
        chunk_row = self.connection.execute("SELECT COUNT(*) AS chunks FROM chunks").fetchone()
        return MemoryStats(
            documents=int(row["documents"]),
            chunks=int(chunk_row["chunks"]),
            estimated_tokens=int(row["characters"]) // 4,
        )
