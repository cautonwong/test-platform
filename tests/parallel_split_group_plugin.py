"""对照组:把一条总线上的一块表拆到另一个 xdist_group,用来证明 mock 能记录到抢占。

默认套件和 tests/conftest.py 都不引用它 —— "分组被拆开"必须是一次刻意的动作
(tests/parallel_bus_check.py 用 `-p` 显式加载),不能藏在代码里悄悄生效。

xdist 会把一条用例上的多个 xdist_group 拼成一个新组名("bus_a" + "split_off_bus_a"
→ "bus_a_split_off_bus_a"),于是这块表的用例和同一总线上的其它用例被派到不同 worker,
两个 worker 各开一条连接去访问同一个 mock —— 真实台架上抢总线的样子。
"""
import pytest

SPLIT_DEVICE = "meter_a2"
SPLIT_GROUP = "split_off_bus_a"


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    """tryfirst 是为了赶在 xdist 把 xdist_group 拼进 nodeid 的钩子之前,理由同 tests/conftest.py。"""
    for item in items:
        if SPLIT_DEVICE in item.nodeid:
            item.add_marker(pytest.mark.xdist_group(SPLIT_GROUP))
