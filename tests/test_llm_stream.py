import asyncio
import json

import httpx
import pytest
from openai import APITimeoutError
from test_llm import message, mock_analyzer

from deadline_assistant.domain import AnalysisValidationError, OutputTooLarge


def chunk(*, delta=None, finish=None, usage=None, choices=True):
    return {
        "id": "mock-stream",
        "created": 1,
        "model": "test-model",
        "object": "chat.completion.chunk",
        "usage": usage,
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}] if choices else [],
    }


def streaming(items):
    body = "".join("data: " + json.dumps(c) + "\n\n" for c in items)
    return httpx.Response(
        200,
        text=body + "data: [DONE]\n\n",
        headers={"content-type": "text/event-stream"},
    )


async def test_stream_assembles_only_content_and_final_usage(configured):
    def handler(request):
        body = json.loads(request.content)
        assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
        return streaming(
            [
                chunk(delta={"role": "assistant", "reasoning_content": "Ignore this reasoning"}),
                chunk(delta={"content": '{"operations":'}),
                chunk(delta={"content": "[]}"}, finish="stop"),
                chunk(
                    choices=False,
                    usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
                ),
            ]
        )

    analyzer = await mock_analyzer(configured.model_copy(update={"llm_stream": True}), handler)
    try:
        result = await analyzer.analyze([message()], [], "Europe/Moscow")
        assert result.result.operations == []
        assert result.token_usage["total_tokens"] == 120
    finally:
        await analyzer.close()


@pytest.mark.parametrize(
    "finish,exception", [(None, AnalysisValidationError), ("length", OutputTooLarge)]
)
async def test_interrupted_or_truncated_stream_is_never_applied_or_retried(
    configured,
    finish,
    exception,
):
    calls = []

    def handler(request):
        calls.append(request)
        return streaming([chunk(delta={"content": '{"operations":[]}'}, finish=finish)])

    analyzer = await mock_analyzer(configured.model_copy(update={"llm_stream": True}), handler)
    try:
        with pytest.raises(exception):
            await analyzer.analyze([message()], [], "Europe/Moscow")
        assert len(calls) == 1
    finally:
        await analyzer.close()


async def test_keepalive_chunks_cannot_extend_request_deadline(configured):
    calls = []
    closed = []

    class KeepAlive(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                yield b": keepalive\n\n"
                await asyncio.sleep(0.005)

        async def aclose(self):
            closed.append(True)

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200, stream=KeepAlive(), headers={"content-type": "text/event-stream"}
        )

    analyzer = await mock_analyzer(
        configured.model_copy(update={"llm_stream": True, "llm_timeout_seconds": 0.05}), handler
    )
    try:
        with pytest.raises(APITimeoutError):
            await analyzer.analyze([message()], [], "Europe/Moscow")
        assert len(calls) == 1 and closed
    finally:
        await analyzer.close()


async def test_gateway_null_created_metadata_does_not_discard_valid_content(configured):
    items = [chunk(delta={"content": '{"operations":[]}'}, finish="stop")]
    items[0]["created"] = None  # Observed in LowRouter's Claude SSE response.
    analyzer = await mock_analyzer(
        configured.model_copy(update={"llm_stream": True}), lambda request: streaming(items)
    )
    try:
        assert (await analyzer.analyze([message()], [], "Europe/Moscow")).result.operations == []
    finally:
        await analyzer.close()
