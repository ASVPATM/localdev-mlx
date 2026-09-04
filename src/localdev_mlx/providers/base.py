from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TypeVar

from pydantic import BaseModel

from localdev_mlx.config import ModelProfile

T = TypeVar("T", bound=BaseModel)


class ProviderError(RuntimeError):
    """Raised when a model provider cannot complete a request."""


class StructuredProvider(ABC):
    @abstractmethod
    def complete_structured(
        self,
        *,
        profile: ModelProfile,
        system_prompt: str,
        user_prompt: str,
        response_model: type[T],
        schema_name: str,
    ) -> T:
        raise NotImplementedError
