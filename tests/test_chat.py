import json
from pathlib import Path

import httpx
import pytest

from maimai_bot.services.chat import ChatService, ChatUnavailable
from maimai_bot.services.web_search import WebSearchService


async def test_chat_keeps_history_and_reset() -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(200, json={"choices": [{"message": {"content": "回答"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ChatService(
            client,
            api_key="key",
            base_url="https://example.test/v1",
            model="model",
            system_prompt="system",
            history_turns=2,
        )
        assert await service.reply("one", "问题一") == "回答"
        assert await service.reply("one", "问题二") == "回答"
        assert len(requests[1]["messages"]) == 4
        service.reset("one")
        await service.reply("one", "问题三")
    assert len(requests[2]["messages"]) == 2


async def test_chat_injects_web_results_and_returns_source_links() -> None:
    llm_requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "search.brave.com":
            return httpx.Response(
                200,
                text=(
                    '{title:"官方更新",url:"https://example.com/update",'
                    'full_title:void 0,description:"新版本已经发布"},'
                ),
            )
        payload = json.loads(request.content)
        llm_requests.append(payload)
        return httpx.Response(200, json={"choices": [{"message": {"content": "查到啦"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ChatService(
            client,
            api_key="key",
            base_url="https://llm.example/v1",
            model="model",
            system_prompt="system",
            web_search=WebSearchService(client),
        )
        answer = await service.reply("one", "联网搜索一下最新版本")

    assert any("实时联网检索结果" in item["content"] for item in llm_requests[0]["messages"])
    assert "https://example.com/update" in answer


async def test_chat_falls_back_when_model_is_not_authorized() -> None:
    used_models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={"data": [{"id": "smart/default"}, {"id": "qwen-chat"}]},
            )
        payload = json.loads(request.content)
        used_models.append(payload["model"])
        if payload["model"] == "not-authorized":
            return httpx.Response(
                403,
                json={
                    "error": {
                        "type": "key_model_access_denied",
                        "code": "403",
                    }
                },
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ChatService(
            client,
            api_key="key",
            base_url="https://example.test/v1",
            model="not-authorized",
            system_prompt="system",
        )
        assert await service.reply("one", "hello") == "OK"
        assert await service.reply("one", "again") == "OK"

    assert used_models == ["not-authorized", "smart/default", "smart/default"]


async def test_chat_persists_memory_and_injects_score_context(tmp_path: Path) -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "记住啦"}}]})

    memory_path = tmp_path / "memory.json"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = ChatService(
            client,
            api_key="key",
            base_url="https://example.test/v1",
            model="model",
            system_prompt="extra",
            memory_path=memory_path,
        )
        await first.reply("conversation", "今天早上去打了舞萌")

        restored = ChatService(
            client,
            api_key="key",
            base_url="https://example.test/v1",
            model="model",
            system_prompt="extra",
            memory_path=memory_path,
        )
        await restored.reply("conversation", "你还记得吗", user_context="DX Rating：15752")

    messages = requests[-1]["messages"]
    assert "藤泽柚子" in messages[0]["content"]
    assert any("今天早上去打了舞萌" in item["content"] for item in messages)
    assert any("DX Rating：15752" in item["content"] for item in messages)
    assert restored.memory_count("conversation") == 2
    restored.reset("conversation")
    assert restored.memory_count("conversation") == 0


async def test_legacy_model_name_uses_responsive_deepseek_deployment() -> None:
    used_models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        used_models.append(json.loads(request.content)["model"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ChatService(
            client,
            api_key="key",
            base_url="https://example.test/v1",
            model="deepseek-v4-flash",
            system_prompt="system",
        )
        assert await service.reply("one", "hello") == "OK"

    assert used_models == ["deepseek-flash"]


async def test_timeout_retries_current_model_once() -> None:
    used_models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        used_models.append(model)
        if len(used_models) == 1:
            raise httpx.ReadTimeout("", request=request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ChatService(
            client,
            api_key="key",
            base_url="https://example.test/v1",
            model="slow-model",
            system_prompt="system",
        )
        assert await service.reply("one", "hello") == "OK"

    assert used_models == ["slow-model", "slow-model"]


async def test_stream_combines_content_but_not_reasoning() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        chunks = [
            {"choices": [{"delta": {"reasoning_content": "private reasoning"}}]},
            {"choices": [{"delta": {"content": "你好"}}]},
            {"choices": [{"delta": {"content": "呀"}, "finish_reason": "stop"}]},
        ]
        body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, text=body + "data: [DONE]\n\n"
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ChatService(
            client,
            api_key="key",
            base_url="https://example.test/v1",
            model="model",
            system_prompt="system",
        )
        assert await service.reply("one", "你好啊") == "你好呀"


async def test_truncated_stream_not_saved_as_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text='data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ChatService(
            client,
            api_key="key",
            base_url="https://example.test/v1",
            model="model",
            system_prompt="system",
        )
        with pytest.raises(ChatUnavailable, match="RemoteProtocolError"):
            await service.reply("one", "你好啊")
        assert not service._history["one"]


async def test_chat_timeout_error_includes_exception_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ChatService(
            client,
            api_key="key",
            base_url="https://example.test/v1",
            model="model",
            system_prompt="system",
        )
        with pytest.raises(ChatUnavailable, match="ReadTimeout"):
            await service.reply("one", "hello")
