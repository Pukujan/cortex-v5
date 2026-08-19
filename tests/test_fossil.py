import httpx

from cortex_v5.fossil import FossilClient


def test_unconfigured_fossil_never_claims_commit():
    client = FossilClient("")
    result = client.commit({"event_type": "claim.proposed"})
    assert result["ok"] is False
    assert result["committed"] is False
    assert result["pending"] is True
    assert result["reason"] == "fossil_not_configured"


def test_down_fossil_is_pending_not_success():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    client = FossilClient("http://fossil.invalid", client=http)
    result = client.propose({"event_type": "claim.proposed"})
    assert result["committed"] is False
    assert result["pending"] is True
    assert result["reason"] == "ConnectError"


def test_propose_response_cannot_masquerade_as_commit():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "committed": True})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = FossilClient("http://fossil.invalid", client=http)
    result = client.propose({"event_type": "claim.proposed"})
    assert result["committed"] is False
    assert result["pending"] is True
    assert result["reason"] == "propose_is_not_commit"
