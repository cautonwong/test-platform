"""WebUI 端到端用例的夹具:拉起 EdgeC 的 webd 行为替身(mock-server),给 HTTP 客户端。

被测服务起不来时 `pytest.skip` 而不是 fail:按平台四档,这属于**测试无效**,
不是产品不合格。跳过理由写在 skip message 里,报告里能看见。
"""
from __future__ import annotations

import pytest

from webui_helpers import Client, start_server


@pytest.fixture(scope="session")
def iems_server():
    server = start_server("iems", 18081)
    if server is None:
        pytest.skip("测试无效:mock-server(iems:18081) 未能启动(检查 Registry 是否已生成)")
    yield server
    server.process.kill()


@pytest.fixture(scope="session")
def dcu_server():
    server = start_server("dcu", 18082)
    if server is None:
        pytest.skip("测试无效:mock-server(dcu:18082) 未能启动(检查 Registry 是否已生成)")
    yield server
    server.process.kill()


@pytest.fixture
def iems(iems_server) -> Client:
    return Client(iems_server.base)


@pytest.fixture
def dcu(dcu_server) -> Client:
    return Client(dcu_server.base)


@pytest.fixture(autouse=True)
def _reset_mock_servers(request):
    """每条用例开跑前把 mock-server 的内存状态恢复成启动快照。

    用例共享一个服务进程:000018 的合闸、000020 的渠道、000007 的改密都会留给下一条,
    随机顺序因此会红而单跑是绿的。先 reset 再跑,顺序才不影响结论。
    """
    for client_name, server_name in (("iems", "iems_server"), ("dcu", "dcu_server")):
        if client_name not in request.fixturenames and server_name not in request.fixturenames:
            continue
        server = request.getfixturevalue(server_name)
        response = Client(server.base).request("POST", "/__test/reset")
        assert response.status == 200, (
            f"mock-server({server.product}) 缺少 /__test/reset;"
            "更新 EdgeC web/mock-server/server.mjs 后再跑这一套"
        )
