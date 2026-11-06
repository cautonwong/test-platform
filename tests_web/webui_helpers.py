"""WebUI 端到端用例的公共部件:被测服务进程、HTTP 客户端、证据写出。

为什么用 HTTP 而不是浏览器:这一层判的是**服务端授权语义**(权限/能力/许可/CSRF/
事务状态机),浏览器只是呈现。浏览器流程由 EdgeC 仓库的 Playwright 用例覆盖
(`web/tests/e2e/`),两边不重复,也不互相替代。

归因口径沿用平台四档:
  - 产品不合格:接口返回了不该返回的东西(2xx 放行、错误码不对、状态机走错)
  - 测试无效:被测服务没起来 / 端口被占 / registry 缺失(环境问题,不是产品问题)
  - 平台错误:用例自身写错(判据不成立)
  - 证据损坏:证据文件缺字段或读不回来
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

EDGEC_WEB = Path(os.environ.get("EDGEC_WEB_DIR", "/tmp/edgec-webui/web"))
EVIDENCE_DIR = Path(os.environ.get("WEBUI_EVIDENCE_DIR", "evidence/webui-e2e"))


def wait_port(port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


@dataclass
class MockServer:
    product: str
    port: int
    process: subprocess.Popen

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def start_server(product: str, port: int) -> MockServer | None:
    registry = EDGEC_WEB / "products" / product / "src" / "generated" / "registry.json"
    if not registry.exists():
        return None
    try:
        process = subprocess.Popen(
            ["node", "mock-server/server.mjs", "--product", product, "--port", str(port)],
            cwd=str(EDGEC_WEB),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None
    if not wait_port(port) or process.poll() is not None:
        if process.poll() is None:
            process.kill()
        return None
    base = f"http://127.0.0.1:{port}"
    try:
        info = Client(base).request("GET", "/api/v1/system/info")
    except Exception:
        process.kill()
        return None
    if info.status != 200 or info.body.get("productId") != product:
        # 端口上已经有一个别的服务 / 另一个 run 的 mock-server:不能把它的状态当成本轮的。
        process.kill()
        return None
    if process.poll() is not None:
        return None
    return MockServer(product=product, port=port, process=process)


@dataclass
class Response:
    status: int
    body: dict
    headers: dict

    @property
    def code(self) -> str:
        return str(self.body.get("code", ""))


class Client:
    """极简 HTTP 客户端:自己管 Cookie;状态变更自己带 Origin + CSRF(服务端强制的)。"""

    def __init__(self, base: str):
        self.base = base
        self.cookies: dict[str, str] = {}
        self.csrf = ""

    def request(self, method: str, path: str, body: dict | None = None, headers: dict | None = None) -> Response:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if self.cookies:
            request.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in self.cookies.items()))
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            request.add_header("Origin", self.base)
            if self.csrf:
                request.add_header("X-CSRF-Token", self.csrf)
        for key, value in (headers or {}).items():
            request.add_header(key, value)

        try:
            with urllib.request.urlopen(request, timeout=10) as raw:
                payload = raw.read().decode() or "{}"
                response = Response(raw.status, _json(payload), dict(raw.headers))
                self._absorb(response)
                return response
        except urllib.error.HTTPError as err:
            return Response(err.code, _json(err.read().decode() or "{}"), dict(err.headers))

    def _absorb(self, response: Response) -> None:
        cookie = response.headers.get("Set-Cookie", "")
        if not cookie:
            return
        pair = cookie.split(";")[0]
        if "=" not in pair:
            return
        name, value = pair.split("=", 1)
        if value:
            self.cookies[name] = value
        else:
            self.cookies.pop(name, None)

    def login(self, username: str, password: str) -> Response:
        response = self.request("POST", "/api/v1/auth/login", {"username": username, "password": password})
        if response.status == 200:
            self.csrf = response.body.get("user", {}).get("csrfToken", "")
        return response


def _json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}


def write_evidence(case_id: str, payload: dict) -> None:
    """一条用例一份 JSON:字段固定,便于 gate 与报告解析。"""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_DIR / f"{case_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
