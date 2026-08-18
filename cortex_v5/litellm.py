"""Strict LiteLLM transport using the OpenAI-compatible streaming API.

Operational timeout contract
----------------------------
V5 always requests streaming completions.  The HTTPX timeout configured here is a
*network inactivity/read* timeout, not a task wall-clock budget and not a model
quality signal.

The currently qualified ckff routes are:

- ``https://ckffai.com/v1`` — preferred Tencent node; ckff reports a 600 second
  network timeout.
- ``https://aws.ckffai.com/v1`` — backup AWS node; ckff reports a 180 second
  network timeout and explicitly warns that non-streaming requests exceeding that
  window fail.

The client default is therefore 600 seconds rather than the historical 120 second
value.  Long model work must still use ``stream=True`` (enforced below) and should
be pre-granulated so one model turn finishes comfortably inside the selected
route's provider ceiling.  A transport timeout must be classified as infrastructure
or route evidence; it must not automatically be interpreted as model incapability.

For the temporary cross-vendor coding-agent execution path while the V5 runtime is
being stabilized, see ``docs/CROSS-VENDOR-OPENCODE.md``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import Any, Final

import httpx

from .contracts import StreamCompletion, ToolCall

EventCallback = Callable[[dict[str, Any]], Awaitable[None] | None]
DEFAULT_STREAM_READ_TIMEOUT_SECONDS: Final[float] = 600.0
_SHORT_IO_TIMEOUT_SECONDS: Final[float] = 30.0
CHAT_COMPLETION_FINISH_REASONS = frozenset(
    {"stop", "length", "tool_calls", "content_filter", "function_call"}
)


class LiteLLMStreamError(RuntimeError):
    """The response violated the Chat Completions streaming protocol."""


def _safe_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Return useful SSE metadata without prompts, content, arguments, or credentials."""
    choices = event.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else {}
    delta = choice.get("delta", {}) if isinstance(choice, Mapping) else {}
    tool_calls = delta.get("tool_calls", []) if isinstance(delta, Mapping) else []
    reason = choice.get("finish_reason") if isinstance(choice, Mapping) else None
    safe_reason = (
        reason if isinstance(reason, str) and reason in CHAT_COMPLETION_FINISH_REASONS else None
    )
    return {
        "event": "litellm.sse",
        "id": event.get("id"),
        "model": event.get("model"),
        "finish_reason": safe_reason,
        "invalid_finish_reason": reason is not None and safe_reason is None,
        "has_content": bool(delta.get("content")) if isinstance(delta, Mapping) else False,
        "tool_call_fragments": len(tool_calls) if isinstance(tool_calls, list) else 0,
        "has_usage": isinstance(event.get("usage"), Mapping),
    }


async def _emit(callback: EventCallback | None, event: dict[str, Any]) -> None:
    if callback is None:
        return
    result = callback(event)
    if result is not None:
        await result


async def parse_sse(
    lines: AsyncIterator[str], *, event_callback: EventCallback | None = None
) -> StreamCompletion:
    """Parse incremental Chat Completions SSE, including fragmented tool calls."""
    content: list[str] = []
    calls: dict[int, dict[str, str]] = {}
    finish_reason: str | None = None
    termination: str | None = None
    event_count = 0
    response_id: str | None = None
    model: str | None = None
    usage: dict[str, Any] = {}
    data_lines: list[str] = []

    async def consume(payload: str) -> bool:
        nonlocal event_count, finish_reason, termination, response_id, model, usage
        if payload.strip() == "[DONE]":
            termination = "done"
            await _emit(event_callback, {"event": "litellm.sse.done"})
            return True
        try:
            event = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise LiteLLMStreamError("LiteLLM stream contained truncated SSE JSON") from exc
        if not isinstance(event, Mapping):
            raise ValueError("LiteLLM SSE event must be a JSON object")
        event_count += 1
        await _emit(event_callback, _safe_event(event))
        response_id = str(event["id"]) if event.get("id") is not None else response_id
        model = str(event["model"]) if event.get("model") is not None else model
        if isinstance(event.get("usage"), Mapping):
            usage = dict(event["usage"])
        choices = event.get("choices", [])
        for choice in choices if isinstance(choices, list) else []:
            if not isinstance(choice, Mapping):
                continue
            reason = choice.get("finish_reason")
            if reason is not None:
                if not isinstance(reason, str) or reason not in CHAT_COMPLETION_FINISH_REASONS:
                    raise LiteLLMStreamError(
                        "LiteLLM stream contained an invalid Chat Completions finish_reason"
                    )
                finish_reason = reason
                termination = "finish_reason"
            delta = choice.get("delta", {})
            if not isinstance(delta, Mapping):
                continue
            fragment = delta.get("content")
            if isinstance(fragment, str):
                content.append(fragment)
            fragments = delta.get("tool_calls", [])
            for fallback_index, item in enumerate(fragments if isinstance(fragments, list) else []):
                if not isinstance(item, Mapping):
                    continue
                index = int(item.get("index", fallback_index))
                call = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                if isinstance(item.get("id"), str):
                    call["id"] += item["id"]
                function = item.get("function", {})
                if isinstance(function, Mapping):
                    if isinstance(function.get("name"), str):
                        call["name"] += function["name"]
                    if isinstance(function.get("arguments"), str):
                        call["arguments"] += function["arguments"]
        return False

    async for raw_line in lines:
        line = raw_line.rstrip("\r")
        if line == "":
            if data_lines and await consume("\n".join(data_lines)):
                data_lines.clear()
                break
            data_lines.clear()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
        # SSE comments (including keepalive pings), event names, ids, and retry
        # fields are deliberately ignored.
    else:
        if data_lines:
            await consume("\n".join(data_lines))

    if termination is None:
        raise LiteLLMStreamError("LiteLLM stream closed before [DONE] or a valid finish_reason")

    parsed_calls: list[ToolCall] = []
    for index in sorted(calls):
        item = calls[index]
        try:
            arguments = json.loads(item["arguments"] or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("LiteLLM completed with malformed tool arguments") from exc
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must decode to a JSON object")
        parsed_calls.append(ToolCall(item["id"], item["name"], arguments))
    return StreamCompletion(
        content="".join(content),
        tool_calls=tuple(parsed_calls),
        finish_reason=finish_reason,
        termination=termination,
        event_count=event_count,
        response_id=response_id,
        model=model,
        usage=usage,
    )


def normalize_models(payload: Any) -> tuple[str, ...]:
    """Normalize OpenAI/LiteLLM model catalogs into stable unique identifiers."""
    rows = (
        payload.get("data", payload.get("models", [])) if isinstance(payload, Mapping) else payload
    )
    if not isinstance(rows, list):
        raise ValueError("LiteLLM model catalog must contain a list")
    names: set[str] = set()
    for row in rows:
        value = (
            row.get("id", row.get("model_name", row.get("model")))
            if isinstance(row, Mapping)
            else row
        )
        if isinstance(value, str) and value.strip():
            names.add(value.strip())
    return tuple(sorted(names))


class LiteLLMClient:
    """Async transport with no hidden retries or alternate invocation paths."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        timeout: float = DEFAULT_STREAM_READ_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
        event_callback: EventCallback | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.base_url = base_url.rstrip("/")
        self.event_callback = event_callback
        self.stream_read_timeout_seconds = float(timeout)
        self._owned = client is None
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        transport_timeout = httpx.Timeout(
            timeout,
            connect=min(timeout, _SHORT_IO_TIMEOUT_SECONDS),
            write=min(timeout, _SHORT_IO_TIMEOUT_SECONDS),
            pool=min(timeout, _SHORT_IO_TIMEOUT_SECONDS),
        )
        self._client = client or httpx.AsyncClient(headers=headers, timeout=transport_timeout)

    def _endpoint(self, path: str) -> str:
        prefix = self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url
        return f"{prefix}/v1/{path.lstrip('/')}"

    async def __aenter__(self) -> LiteLLMClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owned:
            await self._client.aclose()

    async def refresh_models(self) -> tuple[str, ...]:
        response = await self._client.get(self._endpoint("models"))
        response.raise_for_status()
        return normalize_models(response.json())

    async def list_models(self) -> tuple[str, ...]:
        return await self.refresh_models()

    async def chat_completion(
        self,
        *,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        max_tokens: int,
        event_callback: EventCallback | None = None,
        **parameters: Any,
    ) -> StreamCompletion:
        body = dict(parameters)
        body.update(
            model=model,
            messages=list(messages),
            tools=list(tools),
            stream=True,
            max_tokens=max_tokens,
        )
        async with self._client.stream(
            "POST", self._endpoint("chat/completions"), json=body
        ) as response:
            response.raise_for_status()
            return await parse_sse(
                response.aiter_lines(), event_callback=event_callback or self.event_callback
            )

    async def invoke(self, **kwargs: Any) -> StreamCompletion:
        return await self.chat_completion(**kwargs)
