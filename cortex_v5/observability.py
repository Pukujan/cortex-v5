"""Credential-safe local and remote observability for Cortex V5."""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values

from .contracts import SinkResult
from .journal import Journal

REDACTED = "[REDACTED]"
_SECRET_KEY = re.compile(
    r"(?:authorization|proxy-authorization|cookie|set-cookie|api[_-]?key|token|secret|password|passwd|credential|private[_-]?key)",
    re.IGNORECASE,
)
_SECRET_TEXT = re.compile(
    r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+|((?:api[_-]?key|token|secret|password)\s*[=:]\s*)[^\s,;&]+"
)


def sanitize(value: Any, key: str | None = None) -> Any:
    """Recursively return a JSON-safe value with credential material removed."""
    if key and _SECRET_KEY.search(str(key)):
        return REDACTED
    if isinstance(value, Mapping):
        return {str(k): sanitize(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [sanitize(item) for item in value]
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        return _SECRET_TEXT.sub(lambda match: (match.group(1) or match.group(2)) + REDACTED, value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return sanitize(vars(value)) if hasattr(value, "__dict__") else str(value)


def _otlp_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    """Build a minimal OTLP/HTTP JSON trace accepted by standard collectors."""
    event_id = uuid.UUID(str(event["event_id"]))
    trace_id = event_id.hex + uuid.uuid4().hex
    span_id = event_id.hex[:16]
    timestamp = int(float(event["timestamp"]) * 1_000_000_000)
    attributes = [
        {"key": "cortex.task_id", "value": {"stringValue": str(event["task_id"])}},
        {"key": "cortex.event_type", "value": {"stringValue": str(event["event_type"])}},
        {"key": "cortex.event_id", "value": {"stringValue": str(event["event_id"])}},
    ]
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "service.name",
                            "value": {"stringValue": "cortex-v5"},
                        }
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "cortex-v5.runtime"},
                        "spans": [
                            {
                                "traceId": trace_id[:32],
                                "spanId": span_id,
                                "name": str(event["event_type"]),
                                "kind": 1,
                                "startTimeUnixNano": str(timestamp),
                                "endTimeUnixNano": str(timestamp + 1),
                                "attributes": attributes,
                                "status": {"code": 1},
                            }
                        ],
                    }
                ],
            }
        ]
    }


def _langfuse_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    timestamp = datetime.fromtimestamp(float(event["timestamp"]), tz=UTC).isoformat()
    return {
        "batch": [
            {
                "id": uuid.uuid4().hex,
                "type": "trace-create",
                "timestamp": timestamp,
                "body": {
                    "id": str(event["event_id"]),
                    "name": f"cortex-v5:{event['event_type']}",
                    "sessionId": str(event["task_id"]),
                    "tags": ["cortex-v5", str(event["event_type"])],
                    "metadata": event,
                },
            }
        ]
    }


def _repo_environment(repo_root: Path) -> dict[str, str]:
    return {k: str(v) for k, v in dotenv_values(repo_root / ".env").items() if v is not None}


class EventRecorder:
    """Write every event to the TTL-governed journal, then attempt remote sinks.

    SQLite is the only persistent local trace store.  Earlier V5 builds also wrote
    ``events.jsonl``; that duplicate could retain expired events while a quiet runtime
    stayed alive, so it is now treated only as a legacy cleanup location.
    """

    def __init__(
        self,
        journal: Journal,
        *,
        repo_root: str | Path | None = None,
        jsonl_path: str | Path | None = None,
        client: httpx.Client | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.journal = journal
        self.repo_root = Path(repo_root or Path.cwd()).resolve()
        self.env = dict(env) if env is not None else _repo_environment(self.repo_root)
        self.jsonl_path = Path(jsonl_path or journal.data_dir / "events.jsonl")
        self.client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None
        self._remove_legacy_jsonl()

    def _remove_legacy_jsonl(self) -> None:
        """Remove the obsolete duplicate store, failing startup if it cannot be retired."""

        self.jsonl_path.unlink(missing_ok=True)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> EventRecorder:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _post(
        self, url: str | None, payload: dict[str, Any], headers: dict[str, str]
    ) -> tuple[bool, str]:
        if not url:
            return False, "not_configured"
        try:
            response = self.client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            return True, f"http_{response.status_code}"
        except Exception as exc:  # sink failure must never prevent local recording
            return False, type(exc).__name__

    def record(
        self, task_id: str, event_type: str, payload: Any = None, **fields: Any
    ) -> SinkResult:
        event = sanitize(
            {
                "event_id": str(uuid.uuid4()),
                "task_id": str(task_id),
                "event_type": event_type,
                "timestamp": time.time(),
                "payload": {} if payload is None else payload,
                **fields,
            }
        )
        local_ok = False
        local_detail = "ok"
        try:
            self.journal.append_event(str(task_id), event)
            local_ok = True
        except Exception as exc:
            local_detail = type(exc).__name__

        otlp = self.env.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        if not otlp and self.env.get("CORTEX_OTEL_COLLECTOR_HOST"):
            host = self.env["CORTEX_OTEL_COLLECTOR_HOST"].strip()
            port = self.env.get("CORTEX_OTEL_COLLECTOR_HTTP_PORT", "4318").strip()
            scheme = "http" if "://" not in host else ""
            otlp = f"{scheme}://{host}:{port}" if scheme else f"{host}:{port}"
        if otlp:
            otlp = otlp.rstrip("/")
            if not otlp.endswith("/v1/traces"):
                otlp += "/v1/traces"
        grave_ok, grave_detail = self._post(
            otlp,
            _otlp_payload(event),
            {"Content-Type": "application/json"},
        )
        langfuse_host = self.env.get("LANGFUSE_HOST")
        langfuse_url = (
            f"{langfuse_host.rstrip('/')}/api/public/ingestion" if langfuse_host else None
        )
        public = self.env.get("LANGFUSE_PUBLIC_KEY", "")
        secret = self.env.get("LANGFUSE_SECRET_KEY", "")
        auth = httpx.BasicAuth(public, secret) if public or secret else None
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if auth:
            request = httpx.Request("POST", langfuse_url or "http://localhost")
            auth.auth_flow(request).__next__()
            headers["Authorization"] = request.headers["Authorization"]
        lang_ok, lang_detail = self._post(
            langfuse_url,
            _langfuse_payload(event),
            headers,
        )
        result = SinkResult(
            local_ok=local_ok,
            gravebuster_ok=grave_ok,
            langfuse_ok=lang_ok,
            detail=sanitize(
                {"local": local_detail, "gravebuster": grave_detail, "langfuse": lang_detail}
            ),
        )
        receipt = sanitize(
            {"event_id": event["event_id"], "event_type": event_type, **result.to_dict()}
        )
        try:
            self.journal.append_receipt(str(task_id), receipt)
        except Exception:
            pass
        return result
