"""The contract a media provider implements."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field

from ..platforms.base import PlatformError


class MediaError(PlatformError):
    """A media generation call failed."""


@dataclass
class MediaRequest:
    prompt: str
    negative_prompt: str = ""
    kind: str = "image"
    aspect_ratio: str = "1:1"
    width: int = 1080
    height: int = 1080
    duration_seconds: float = 0.0
    model: str | None = None
    reference_image_url: str | None = None
    count: int = 1
    extra: dict = field(default_factory=dict)


@dataclass
class MediaResult:
    task_id: str
    urls: list[str] = field(default_factory=list)
    state: str = "success"
    model: str = ""
    provider: str = ""
    error: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.state == "success" and bool(self.urls)


class MediaProvider(abc.ABC):
    """Generates images and videos."""

    name: str

    @abc.abstractmethod
    def submit(self, request: MediaRequest) -> str:
        """Start a generation and return the provider's task id."""

    @abc.abstractmethod
    def poll(self, task_id: str) -> MediaResult:
        """Check a task once, without blocking."""

    @abc.abstractmethod
    def generate(self, request: MediaRequest) -> MediaResult:
        """Submit and wait for the result."""

    def health_check(self) -> dict:
        return {"provider": self.name, "ok": True}
