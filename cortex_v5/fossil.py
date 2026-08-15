"""FOSSIL client. Cortex asks; it never reaches into FOSSIL storage.

Until a local FOSSIL serve URL is configured and healthy, every mutating call
returns an explicit pending/uncommitted result. That is not success.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import httpx


class FossilClient:
    """Thin HTTP client for search/read/lineage/propose/validate/commit."""

    def __init__(
        self,
        base_url: str = "",
        *,
        timeout: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = client
        self._owns_client = client is None

    def configured(self) -> bool:
        return bool(self.base_url)

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def _unavailable(self, action: str, detail: str) -> dict[str, Any]:
        return {
            "ok": False,
            "committed": False,
            "pending": True,
            "action": action,
            "reason": detail,
            "authority": "uncommitted",
        }

    def _request(self, action: str, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not self.configured():
            return self._unavailable(action, "fossil_not_configured")
        try:
            response = self._http().post(f"{self.base_url}{path}", json=dict(payload))
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            return self._unavailable(action, type(exc).__name__)
        if not isinstance(body, dict):
            return self._unavailable(action, "malformed_fossil_response")
        result = dict(body)
        result.setdefault("ok", False)
        result.setdefault("committed", False)
        result.setdefault("pending", not bool(result.get("committed")))
        result.setdefault("action", action)
        if result.get("committed") and action in {"propose", "validate"}:
            result["committed"] = False
            result["pending"] = True
            result["reason"] = "propose_is_not_commit"
        return result

    def search(self, query: str, *, pack_ids: Sequence[str] = ()) -> dict[str, Any]:
        return self._request("search", "/v1/search", {"query": query, "pack_ids": list(pack_ids)})

    def read(self, stable_id: str) -> dict[str, Any]:
        return self._request("read", "/v1/read", {"id": stable_id})

    def lineage(self, stable_id: str) -> dict[str, Any]:
        return self._request("lineage", "/v1/lineage", {"id": stable_id})

    def propose(self, event: Mapping[str, Any]) -> dict[str, Any]:
        return self._request("propose", "/v1/propose", {"event": dict(event)})

    def validate(self, event: Mapping[str, Any]) -> dict[str, Any]:
        return self._request("validate", "/v1/validate", {"event": dict(event)})

    def commit(self, event: Mapping[str, Any]) -> dict[str, Any]:
        result = self._request("commit", "/v1/commit", {"event": dict(event)})
        if result.get("pending"):
            result["committed"] = False
        return result
