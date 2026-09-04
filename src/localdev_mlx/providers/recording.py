from __future__ import annotations

import hashlib
import time

from pydantic import ValidationError

from localdev_mlx.execution.budget import DeadlineExceeded, deadline
from localdev_mlx.providers.base import ProviderError, StructuredProvider
from localdev_mlx.schemas import AttemptRecord, utc_now


class RecordingProvider(StructuredProvider):
    """One durable record and hard deadline for every actual model request."""

    def __init__(self, provider, store, task, progress):
        self.provider, self.store, self.task, self.progress = provider, store, task, progress
        self.manifest_path = None
        self.work_unit = None
        self.seen: set[str] = set()

    def complete_structured(
        self, *, profile, system_prompt, user_prompt, response_model, schema_name
    ):
        prompt = system_prompt + "\n" + user_prompt
        digest = hashlib.sha256((profile.model + schema_name + prompt).encode()).hexdigest()
        if digest in self.seen:
            raise ProviderError(
                "Identical effective prompt already sent to this model; stopped duplicate call"
            )
        self.seen.add(digest)
        prefix = f"call-{len(self.task.attempt_records) + 1:03d}"
        record = AttemptRecord(
            phase=self.task.phase,
            work_unit=self.work_unit,
            model=profile.model,
            profile=profile.name,
            prompt_chars=len(prompt),
            prompt_hash=digest,
            context_manifest_path=self.manifest_path,
            thinking_budget=profile.thinking_budget,
            max_tokens=profile.max_tokens,
            timeout_seconds=profile.request_timeout_seconds,
        )
        self.task.attempt_records.append(record)
        self.store.write_text(self.task.id, f"{prefix}-prompt.txt", prompt)
        self.store.save(self.task)
        self.progress.emit(
            f"[{self.task.id}] {self.task.phase.value.upper()} model={profile.model} "
            f"prompt={len(prompt):,} chars deadline={profile.request_timeout_seconds:g}s"
        )
        started = time.monotonic()
        try:
            with deadline(profile.request_timeout_seconds, label=f"{schema_name} request"):
                result = self.provider.complete_structured(
                    profile=profile,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    response_model=response_model,
                    schema_name=schema_name,
                )
            result = response_model.model_validate(result.model_dump())
            record.response_schema_valid = True
            record.response_digest = hashlib.sha256(result.model_dump_json().encode()).hexdigest()
            record.response_path = str(
                self.store.write_json(self.task.id, f"{prefix}-response.json", result)
            )
            record.outcome = "response"
            record.usage = getattr(self.provider, "last_metadata", {}).copy()
            return result
        except (DeadlineExceeded, ValidationError) as exc:
            record.outcome = "timeout" if isinstance(exc, DeadlineExceeded) else "invalid_response"
            record.failure_category = record.outcome
            record.response_path = str(
                self.store.write_text(self.task.id, f"{prefix}-error.txt", str(exc))
            )
            if isinstance(exc, DeadlineExceeded) and str(exc).startswith("Whole task"):
                raise
            raise ProviderError(str(exc)) from exc
        except BaseException as exc:
            record.outcome = "cancelled" if isinstance(exc, KeyboardInterrupt) else "provider_error"
            record.failure_category = record.outcome
            record.response_path = str(
                self.store.write_text(self.task.id, f"{prefix}-error.txt", str(exc))
            )
            raise
        finally:
            record.ended_at = utc_now()
            record.elapsed_seconds = round(time.monotonic() - started, 3)
            self.store.save(self.task)
