from __future__ import annotations

import json
import re
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from localdev_mlx.config import ModelProfile
from localdev_mlx.execution.budget import deadline
from localdev_mlx.providers.base import ProviderError, StructuredProvider

T = TypeVar("T", bound=BaseModel)


class MLXOpenAIProvider(StructuredProvider):
    """Call mlx_vlm.server through its OpenAI-compatible chat endpoint."""

    def __init__(self, *, api_key: str = "local-not-required") -> None:
        self.api_key = api_key
        self.last_metadata: dict[str, Any] = {}

    def list_models(self, profile: ModelProfile) -> list[str]:
        try:
            response = httpx.get(
                f"http://{profile.host}:{profile.port}/v1/models",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"Unable to query MLX models: {exc}") from exc
        values = payload.get("data", []) if isinstance(payload, dict) else []
        return [str(item.get("id")) for item in values if isinstance(item, dict) and item.get("id")]

    def complete_structured(
        self,
        *,
        profile: ModelProfile,
        system_prompt: str,
        user_prompt: str,
        response_model: type[T],
        schema_name: str,
    ) -> T:
        self.last_metadata = {}
        schema = response_model.model_json_schema()
        payload: dict[str, Any] = {
            "model": profile.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
            "max_tokens": profile.max_tokens,
            "stream": False,
            "enable_thinking": profile.enable_thinking,
            "thinking_budget": profile.thinking_budget,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        try:
            with (
                deadline(profile.request_timeout_seconds, label=f"{schema_name} request"),
                httpx.Client(
                    base_url=profile.base_url,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    timeout=httpx.Timeout(
                        connect=min(10, profile.request_timeout_seconds),
                        read=profile.request_timeout_seconds,
                        write=min(30, profile.request_timeout_seconds),
                        pool=5,
                    ),
                    trust_env=False,
                ) as client,
            ):
                response = client.post("/chat/completions", json=payload)
                response.raise_for_status()
                body = response.json()
                self.last_metadata = {
                    "usage": body.get("usage", {}),
                    "finish_reason": body.get("choices", [{}])[0].get("finish_reason"),
                }
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"MLX completion failed: {exc}") from exc

        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"Unexpected MLX response shape: {body!r}") from exc
        if not isinstance(content, str):
            raise ProviderError("MLX response content was not text")

        cleaned = _extract_json(content)
        try:
            return response_model.model_validate_json(cleaned)
        except ValidationError as exc:
            raise ProviderError(
                "MLX returned content that did not satisfy the response schema. "
                f"Validation error: {exc}\nRaw content: {content[:4000]}"
            ) from exc


def _extract_json(content: str) -> str:
    text = content.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        pass
    start = min((index for index in (text.find("{"), text.find("[")) if index >= 0), default=-1)
    if start < 0:
        return text
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return text
