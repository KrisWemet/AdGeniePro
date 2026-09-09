"""kie.ai media generation client.

kie.ai fronts several image and video models (Nano Banana, Flux, Veo, Kling,
Seedance and others) behind one asynchronous job API:

    POST /api/v1/jobs/createTask   -> {"data": {"taskId": ...}}
    GET  /api/v1/jobs/recordInfo   -> {"data": {"state": ..., "resultJson": ...}}

Two details shape this client. The response envelope differs between the
unified jobs API and the older per-model endpoints, so both shapes are parsed
rather than one being assumed. And the result URLs expire in about a day, which
is why `generate` returns URLs for the caller to persist immediately rather
than storing them as if they were durable.
"""

from __future__ import annotations

import json
import logging
import time

import httpx

from ..config import Settings, get_settings
from .base import MediaError, MediaProvider, MediaRequest, MediaResult

logger = logging.getLogger(__name__)

__all__ = ["KieClient"]

# States the API reports. Anything unrecognised is treated as still running,
# because failing a task that is merely in a new state loses paid work.
TERMINAL_SUCCESS = {"success", "succeeded", "completed"}
TERMINAL_FAILURE = {"fail", "failed", "error", "cancelled", "canceled"}

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


class KieClient(MediaProvider):
    name = "kie"

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        if not self.settings.kie_api_key and client is None:
            raise MediaError(
                "kie.ai media generation needs an API key (set KIE_API_KEY).",
                code="NO_CREDENTIALS",
            )
        self.base_url = self.settings.kie_base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=120.0)
        self.calls: list[tuple[str, dict]] = []

    # -- transport -------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.kie_api_key}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.base_url}/{path.lstrip('/')}"
        last: MediaError | None = None
        for attempt in range(4):
            try:
                response = self._client.request(
                    method, url, headers=self._headers(), **kwargs
                )
            except httpx.HTTPError as exc:
                last = MediaError(
                    f"network error calling kie.ai: {exc}", retryable=True
                )
            else:
                if response.status_code < 400:
                    try:
                        body = response.json() if response.content else {}
                    except ValueError as exc:
                        # A gateway page can answer 200 with HTML. Letting the
                        # decode error escape would leave the asset stuck in
                        # GENERATING, since callers only catch MediaError.
                        raise MediaError(
                            f"kie.ai returned a non-JSON response: {exc}",
                            code="BAD_RESPONSE",
                            retryable=True,
                            payload={"body": response.text[:300]},
                        ) from exc
                    self.calls.append((f"{method} {path}", kwargs.get("json", {})))
                    return self._unwrap(body)
                last = self._to_error(response)
            if not last.retryable or attempt == 3:
                raise last
            time.sleep(2**attempt)
        raise last  # pragma: no cover

    @staticmethod
    def _unwrap(body: dict) -> dict:
        """Return the payload, raising on a non-zero application code.

        kie.ai answers HTTP 200 with an error code inside the envelope, so the
        status line alone is not enough to tell success from failure.
        """
        if not isinstance(body, dict):
            return {}
        code = body.get("code")
        if code is not None and int(code) not in (0, 200):
            raise MediaError(
                f"kie.ai error {code}: {body.get('msg') or body.get('message') or ''}",
                code=code,
                retryable=int(code) in (429, 500, 502, 503),
                payload=body,
            )
        data = body.get("data")
        return data if isinstance(data, dict) else body

    def _to_error(self, response: httpx.Response) -> MediaError:
        try:
            body = response.json()
        except ValueError:
            body = {"msg": response.text[:400]}
        message = body.get("msg") or body.get("message") or response.text[:300]
        hint = ""
        if response.status_code == 402:
            hint = " The kie.ai account is out of credit."
        elif response.status_code == 401:
            hint = " Check KIE_API_KEY."
        return MediaError(
            f"kie.ai error {response.status_code}: {message}{hint}",
            code=response.status_code,
            retryable=response.status_code in RETRYABLE_STATUS,
            payload=body if isinstance(body, dict) else {},
        )

    # -- generation ------------------------------------------------------
    def _model_for(self, request: MediaRequest) -> str:
        if request.model:
            return request.model
        return (
            self.settings.kie_video_model
            if request.kind == "video"
            else self.settings.kie_image_model
        )

    def _build_input(self, request: MediaRequest) -> dict:
        payload: dict = {"prompt": request.prompt}
        if request.negative_prompt:
            payload["negative_prompt"] = request.negative_prompt
        if request.aspect_ratio:
            payload["aspect_ratio"] = request.aspect_ratio
        if request.kind == "image":
            payload["output_format"] = request.extra.get("output_format", "png")
            if request.count > 1:
                payload["num_images"] = request.count
        else:
            if request.duration_seconds:
                payload["duration"] = int(round(request.duration_seconds))
            payload["enable_audio"] = request.extra.get("enable_audio", False)
        if request.reference_image_url:
            payload["image_urls"] = [request.reference_image_url]
        payload.update(request.extra.get("input", {}))
        return payload

    def submit(self, request: MediaRequest) -> str:
        model = self._model_for(request)
        body: dict = {"model": model, "input": self._build_input(request)}
        if request.extra.get("callback_url"):
            body["callBackUrl"] = request.extra["callback_url"]

        data = self._request("POST", "/api/v1/jobs/createTask", json=body)
        task_id = data.get("taskId") or data.get("task_id") or data.get("id")
        if not task_id:
            raise MediaError(
                f"kie.ai accepted the request but returned no task id: {data}",
                code="NO_TASK_ID",
                payload=data,
            )
        logger.info("kie.ai task %s submitted (%s)", task_id, model)
        return str(task_id)

    def poll(self, task_id: str) -> MediaResult:
        data = self._request(
            "GET", "/api/v1/jobs/recordInfo", params={"taskId": task_id}
        )
        return self._parse_result(task_id, data)

    def _parse_result(self, task_id: str, data: dict) -> MediaResult:
        """Read either envelope shape.

        The unified jobs API reports `state` with URLs inside a `resultJson`
        string; the older per-model endpoints report `successFlag` with URLs in
        a nested `response` object.
        """
        state = str(data.get("state") or data.get("status") or "").lower()
        if not state and "successFlag" in data:
            flag = data.get("successFlag")
            state = "success" if flag in (1, "1", True) else "generating"
            if data.get("errorCode") or data.get("failCode"):
                state = "fail"

        urls = _extract_urls(data)
        if urls and state not in TERMINAL_FAILURE:
            state = "success"

        error = None
        if state in TERMINAL_FAILURE:
            error = (
                data.get("failMsg")
                or data.get("errorMessage")
                or data.get("msg")
                or "generation failed"
            )

        return MediaResult(
            task_id=task_id,
            urls=urls,
            state=state or "generating",
            model=str(data.get("model") or ""),
            provider=self.name,
            error=error,
            raw=data,
        )

    def generate(self, request: MediaRequest) -> MediaResult:
        """Submit and wait. Blocks for up to the configured timeout."""
        task_id = self.submit(request)
        deadline = time.monotonic() + self.settings.kie_poll_timeout_seconds
        interval = max(1.0, self.settings.kie_poll_interval_seconds)

        while time.monotonic() < deadline:
            time.sleep(interval)
            result = self.poll(task_id)
            if result.state in TERMINAL_SUCCESS:
                if not result.urls:
                    raise MediaError(
                        f"kie.ai task {task_id} reported success with no output",
                        code="EMPTY_RESULT",
                        payload=result.raw,
                    )
                logger.info("kie.ai task %s produced %s asset(s)", task_id, len(result.urls))
                return result
            if result.state in TERMINAL_FAILURE:
                raise MediaError(
                    f"kie.ai task {task_id} failed: {result.error}",
                    code="GENERATION_FAILED",
                    payload=result.raw,
                )

        raise MediaError(
            f"kie.ai task {task_id} did not finish within "
            f"{self.settings.kie_poll_timeout_seconds:.0f}s. It may still complete; "
            "poll the task id rather than resubmitting, which would be charged again.",
            code="TIMEOUT",
            retryable=True,
            payload={"task_id": task_id},
        )

    def health_check(self) -> dict:
        return {
            "provider": self.name,
            "ok": bool(self.settings.kie_api_key),
            "image_model": self.settings.kie_image_model,
            "video_model": self.settings.kie_video_model,
        }


def _extract_urls(data: dict) -> list[str]:
    """Pull result URLs out of whichever shape this response uses."""
    urls: list[str] = []

    result_json = data.get("resultJson") or data.get("result_json")
    if isinstance(result_json, str) and result_json.strip():
        try:
            result_json = json.loads(result_json)
        except json.JSONDecodeError:
            result_json = None
    for container in (result_json, data.get("response"), data):
        if not isinstance(container, dict):
            continue
        for key in ("resultUrls", "result_urls", "fullResultUrls", "originUrls", "urls"):
            value = container.get(key)
            if isinstance(value, str):
                urls.append(value)
            elif isinstance(value, list):
                urls.extend(str(v) for v in value if v)
        for key in ("resultUrl", "imageUrl", "videoUrl", "url"):
            value = container.get(key)
            if isinstance(value, str) and value.startswith("http"):
                urls.append(value)
        if urls:
            break

    seen: set[str] = set()
    return [u for u in urls if u.startswith("http") and not (u in seen or seen.add(u))]
