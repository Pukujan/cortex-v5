import json

import httpx
import pytest

from cortex_v5.litellm import (
    LiteLLMClient,
    LiteLLMStreamError,
    normalize_models,
    parse_sse,
)


async def lines(*values):
    for value in values:
        yield value


def test_normalizes_dynamic_catalog_shapes():
    assert normalize_models({"data": [{"id": "z"}, {"model_name": "a"}, {"id": "z"}]}) == (
        "a",
        "z",
    )
    assert normalize_models({"models": ["b", {"model": "a"}]}) == ("a", "b")


@pytest.mark.asyncio
async def test_fragmented_content_tools_and_finish_without_done():
    events = []
    chunks = [
        {"id": "r", "model": "m", "choices": [{"delta": {"content": "hi "}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_",
                                "function": {"name": "lo", "arguments": '{"x":'},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "id": "1", "function": {"name": "ok", "arguments": "1}"}}
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"x": 2},
        },
    ]
    stream = []
    for chunk in chunks:
        stream += [f"data: {json.dumps(chunk)}", ""]
    result = await parse_sse(lines(*stream), event_callback=events.append)
    assert result.content == "hi "
    assert result.tool_calls[0].call_id == "call_1"
    assert result.tool_calls[0].name == "look"
    assert result.tool_calls[0].arguments == {"x": 1}
    assert result.termination == "finish_reason"
    assert result.usage == {"x": 2}
    assert all("content" not in event and "arguments" not in event for event in events)


@pytest.mark.asyncio
async def test_done_is_valid_and_bare_connection_close_is_protocol_error():
    done = await parse_sse(lines("data: [DONE]", ""))
    assert done.termination == "done"
    with pytest.raises(LiteLLMStreamError, match="closed before"):
        await parse_sse(lines())


@pytest.mark.asyncio
async def test_partial_event_then_connection_close_is_protocol_error():
    partial = json.dumps({"choices": [{"delta": {"content": "unfinished"}}]})
    with pytest.raises(LiteLLMStreamError, match="finish_reason"):
        await parse_sse(lines(f"data: {partial}", ""))


@pytest.mark.asyncio
async def test_connection_close_mid_json_is_protocol_error():
    with pytest.raises(LiteLLMStreamError, match="truncated SSE JSON"):
        await parse_sse(lines('data: {"choices": ['))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason", ["stop", "length", "tool_calls", "content_filter", "function_call"]
)
async def test_all_chat_completions_finish_reasons_are_terminal(reason):
    event = json.dumps({"choices": [{"delta": {}, "finish_reason": reason}]})
    result = await parse_sse(lines(f"data: {event}", ""))
    assert result.finish_reason == reason
    assert result.termination == "finish_reason"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["nonsense", "", "complete", 1, False, {"stop": True}])
async def test_unknown_non_null_finish_reason_is_protocol_error(reason):
    event = json.dumps({"choices": [{"delta": {}, "finish_reason": reason}]})
    with pytest.raises(LiteLLMStreamError, match="invalid Chat Completions finish_reason"):
        await parse_sse(lines(f"data: {event}", "", "data: [DONE]", ""))


@pytest.mark.asyncio
async def test_null_finish_reason_is_nonterminal_until_done():
    event = json.dumps({"choices": [{"delta": {"content": "ok"}, "finish_reason": None}]})
    result = await parse_sse(lines(f"data: {event}", "", "data: [DONE]", ""))
    assert result.content == "ok"
    assert result.finish_reason is None
    assert result.termination == "done"


@pytest.mark.asyncio
async def test_client_always_gets_live_catalog_and_posts_strict_stream_path():
    seen = []

    def handler(request):
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": f"m{len(seen)}"}]})
        body = json.loads(request.content)
        assert request.url.path == "/v1/chat/completions"
        assert {"model", "tools", "stream", "max_tokens"} <= body.keys()
        assert body["stream"] is True
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n',
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = LiteLLMClient("http://lite", client=http)
        assert await client.refresh_models() == ("m1",)
        assert await client.refresh_models() == ("m2",)
        result = await client.invoke(model="m", messages=[], tools=[], max_tokens=10)
    assert result.content == "ok"
    assert [request.method for request in seen] == ["GET", "GET", "POST"]
