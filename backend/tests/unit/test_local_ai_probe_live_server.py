"""The probe against a real HTTP server.

`check_ollama` is not stubbed here: a threaded server listens on a loopback port
and speaks Ollama's `/api/tags` protocol, so the test covers the actual socket,
timeout, JSON parsing and model-name matching that decide whether a job may run.

The server is a *protocol* stand-in for the daemon (it has no weights); it proves
the client half of the contract, which is the half that breaks on Windows when
Ollama is bound to the wrong interface.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src import local_ai


def start_fake_ollama(models: list[str] | None = None, status: int = 200, body: bytes | None = None):
    payload = json.dumps({"models": [{"name": name} for name in (models or [])]}).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib naming
            if self.path != "/api/tags":
                self.send_response(404)
                self.end_headers()
                return
            data = body if body is not None else payload
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):  # silence the server in test output
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture()
def ollama_server(monkeypatch):
    servers = []

    def _start(**kwargs):
        server, url = start_fake_ollama(**kwargs)
        servers.append(server)
        monkeypatch.setenv("OLLAMA_BASE_URL", f"{url}/v1")
        monkeypatch.setenv("LLM", "ollama:llama3.2:3b")
        return url

    yield _start
    for server in servers:
        server.shutdown()


def test_probe_reaches_a_live_loopback_daemon(ollama_server):
    url = ollama_server(models=["llama3.2:3b"])

    status = local_ai.check_ollama()

    assert status["reachable"] is True
    assert status["base_url"] == url
    assert status["models"] == ["llama3.2:3b"]


def test_probe_reports_connection_refused_clearly(monkeypatch):
    # Nothing is listening here.
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9/v1")

    status = local_ai.check_ollama()

    assert status["reachable"] is False
    assert status["error"]


def test_installed_model_is_matched_against_the_live_list(ollama_server):
    ollama_server(models=["qwen2.5:7b", "llama3.2:3b"])

    status = local_ai.llm_status()

    assert status["model_available"] is True
    assert status["reachable"] is True
    assert "ollama pull" not in (status["message"] or "")


def test_missing_model_names_the_pull_command(ollama_server):
    ollama_server(models=["qwen2.5:7b"])

    status = local_ai.llm_status()

    assert status["model_available"] is False
    assert "ollama pull llama3.2:3b" in status["message"]


def test_malformed_json_is_reported_not_crashed(ollama_server):
    ollama_server(body=b"{not json")

    status = local_ai.check_ollama()

    assert status["reachable"] is True
    assert "unreadable" in (status["error"] or "").lower()


def test_http_error_is_reported_as_unreachable(ollama_server):
    ollama_server(status=500, body=b"{}")

    status = local_ai.check_ollama()

    assert status["reachable"] is False
    assert status["error"]


def test_ensure_ollama_ready_raises_when_the_model_is_absent(ollama_server):
    ollama_server(models=[])

    with pytest.raises(local_ai.LocalModelUnavailable) as error:
        local_ai.ensure_ollama_ready()

    assert "ollama pull llama3.2:3b" in str(error.value)
