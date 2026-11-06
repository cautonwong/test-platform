"""WebUI 测试夹具:端口上的服务必须真是这次起的 mock-server。"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests_web"))

import webui_helpers  # noqa: E402


class FakeProcess:
    def __init__(self):
        self.killed = False

    def poll(self):
        return None

    def kill(self):
        self.killed = True


class FakeClient:
    def __init__(self, response):
        self.response = response

    def request(self, method, path):
        assert (method, path) == ("GET", "/api/v1/system/info")
        return self.response


def _registry(tmp_path, product="iems"):
    registry = tmp_path / "products" / product / "src" / "generated" / "registry.json"
    registry.parent.mkdir(parents=True)
    registry.write_text("{}", encoding="utf-8")
    return tmp_path


def test_start_server_rejects_a_foreign_product_on_the_port(tmp_path, monkeypatch):
    fake = FakeProcess()
    monkeypatch.setattr(webui_helpers, "EDGEC_WEB", _registry(tmp_path))
    monkeypatch.setattr(webui_helpers.subprocess, "Popen", lambda *args, **kwargs: fake)
    monkeypatch.setattr(webui_helpers, "wait_port", lambda port: True)
    monkeypatch.setattr(
        webui_helpers,
        "Client",
        lambda base: FakeClient(type("R", (), {"status": 200, "body": {"productId": "dcu"}})()),
    )

    assert webui_helpers.start_server("iems", 18081) is None
    assert fake.killed


def test_start_server_returns_none_when_node_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(webui_helpers, "EDGEC_WEB", _registry(tmp_path))

    def missing(*args, **kwargs):
        raise FileNotFoundError("node")

    monkeypatch.setattr(webui_helpers.subprocess, "Popen", missing)

    assert webui_helpers.start_server("iems", 18081) is None


def test_start_server_accepts_the_matching_product(tmp_path, monkeypatch):
    fake = FakeProcess()
    monkeypatch.setattr(webui_helpers, "EDGEC_WEB", _registry(tmp_path))
    monkeypatch.setattr(webui_helpers.subprocess, "Popen", lambda *args, **kwargs: fake)
    monkeypatch.setattr(webui_helpers, "wait_port", lambda port: True)
    monkeypatch.setattr(
        webui_helpers,
        "Client",
        lambda base: FakeClient(type("R", (), {"status": 200, "body": {"productId": "iems"}})()),
    )

    server = webui_helpers.start_server("iems", 18081)

    assert server is not None and server.process is fake
    assert not fake.killed
