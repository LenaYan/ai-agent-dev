"""用一个本地假 OpenAI 兼容服务器验证 HTTP 客户端。

没有真实 key 也要验证这层：它是唯一碰真实线上协议的代码。
断言分两头 —— 发出去的请求长得对（tools 带上了），收回来的解析得对。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from toolloop.llm import LLMError, OpenAICompatClient
from toolloop.tools import TOOL_SCHEMAS

RECEIVED: list[dict[str, Any]] = []

TOOL_CALL_RESPONSE = {
    "choices": [
        {
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_abc",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": '{"path": "a.txt", "content": "hi"}',
                        },
                    }
                ],
            }
        }
    ]
}


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        RECEIVED.append({"path": self.path, "body": body, "auth": self.headers.get("Authorization")})
        if self.path.endswith("/boom/chat/completions"):
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"internal error")
            return
        payload = json.dumps(TOOL_CALL_RESPONSE).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:
        return


@pytest.fixture(scope="module")
def server() -> Any:
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_request_shape_and_tool_call_parsing(server: str) -> None:
    RECEIVED.clear()
    client = OpenAICompatClient(model="fake-model", base_url=server, api_key="secret")
    reply = client.chat([{"role": "user", "content": "写 a.txt"}], TOOL_SCHEMAS)

    sent = RECEIVED[-1]
    assert sent["path"] == "/chat/completions"
    assert sent["auth"] == "Bearer secret"
    assert sent["body"]["model"] == "fake-model"
    assert [t["function"]["name"] for t in sent["body"]["tools"]] == [
        "list_files",
        "read_file",
        "write_file",
    ]

    assert len(reply.tool_calls) == 1
    call = reply.tool_calls[0]
    assert (call.id, call.name) == ("call_abc", "write_file")
    assert json.loads(call.arguments)["path"] == "a.txt"
    # raw 必须是可以原样塞回 messages 的 assistant 消息
    assert reply.raw["role"] == "assistant" and "tool_calls" in reply.raw


def test_http_error_becomes_llm_error(server: str) -> None:
    client = OpenAICompatClient(model="m", base_url=f"{server}/boom")
    with pytest.raises(LLMError, match="HTTP 500"):
        client.chat([{"role": "user", "content": "x"}], TOOL_SCHEMAS)


def test_unreachable_backend_becomes_llm_error() -> None:
    client = OpenAICompatClient(model="m", base_url="http://127.0.0.1:1", timeout=2)
    with pytest.raises(LLMError, match="连不上"):
        client.chat([{"role": "user", "content": "x"}], TOOL_SCHEMAS)
