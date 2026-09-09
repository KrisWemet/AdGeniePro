"""A media provider that generates locally, with no API key and no cost.

Its job is not to make good images. It is to let the whole pipeline, including
asset storage, database rows and the platform upload path, be exercised and
tested end to end. It renders a deterministic placeholder at the exact
dimensions the real placement uses, so a layout or aspect-ratio mistake still
shows up.
"""

from __future__ import annotations

import hashlib
import zlib
from dataclasses import dataclass

from .base import MediaProvider, MediaRequest, MediaResult

__all__ = ["SandboxMediaProvider", "render_placeholder_png"]


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        len(payload).to_bytes(4, "big")
        + kind
        + payload
        + zlib.crc32(kind + payload).to_bytes(4, "big")
    )


def render_placeholder_png(width: int, height: int, seed: str) -> bytes:
    """A real PNG of the requested size, coloured deterministically by seed.

    Hand-encoded rather than pulled from an imaging library so the sandbox adds
    no dependency to a project that otherwise needs none.
    """
    digest = hashlib.sha256(seed.encode()).digest()
    base = (digest[0] // 2 + 60, digest[1] // 2 + 60, digest[2] // 2 + 60)

    rows = bytearray()
    for y in range(height):
        rows.append(0)  # per-scanline filter type
        shade = y / max(1, height - 1)
        pixel = bytes(
            min(255, int(channel * (0.72 + 0.4 * shade))) for channel in base
        )
        rows.extend(pixel * width)

    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(
            b"IHDR",
            width.to_bytes(4, "big")
            + height.to_bytes(4, "big")
            + bytes([8, 2, 0, 0, 0]),  # 8-bit truecolour
        )
        + _chunk(b"IDAT", zlib.compress(bytes(rows), 6))
        + _chunk(b"IEND", b"")
    )


@dataclass
class _Task:
    request: MediaRequest
    polls: int = 0


class SandboxMediaProvider(MediaProvider):
    name = "sandbox"

    def __init__(self, polls_before_ready: int = 0, fail: bool = False) -> None:
        # A non-zero value makes tasks report "generating" for a while, so the
        # polling loop is genuinely exercised rather than short-circuited.
        self.polls_before_ready = polls_before_ready
        self.fail = fail
        self.tasks: dict[str, _Task] = {}
        self.generated: list[MediaRequest] = []
        self._counter = 0

    def submit(self, request: MediaRequest) -> str:
        self._counter += 1
        task_id = f"sandbox-{self._counter:06d}"
        self.tasks[task_id] = _Task(request=request)
        self.generated.append(request)
        return task_id

    def poll(self, task_id: str) -> MediaResult:
        task = self.tasks.get(task_id)
        if task is None:
            return MediaResult(
                task_id=task_id, state="fail", error="unknown task", provider=self.name
            )
        if self.fail:
            return MediaResult(
                task_id=task_id,
                state="fail",
                error="simulated generation failure",
                provider=self.name,
            )
        task.polls += 1
        if task.polls <= self.polls_before_ready:
            return MediaResult(task_id=task_id, state="generating", provider=self.name)

        extension = "mp4" if task.request.kind == "video" else "png"
        return MediaResult(
            task_id=task_id,
            state="success",
            urls=[f"sandbox://{task_id}.{extension}"],
            model=task.request.model or f"sandbox-{task.request.kind}",
            provider=self.name,
            raw={"width": task.request.width, "height": task.request.height},
        )

    def generate(self, request: MediaRequest) -> MediaResult:
        task_id = self.submit(request)
        result = self.poll(task_id)
        while result.state == "generating":
            result = self.poll(task_id)
        return result

    def render(self, request: MediaRequest) -> bytes:
        """The bytes a sandbox URL stands for."""
        return render_placeholder_png(
            request.width or 1080, request.height or 1080, request.prompt
        )

    def health_check(self) -> dict:
        return {"provider": self.name, "ok": True, "mode": "sandbox", "cost": 0}
