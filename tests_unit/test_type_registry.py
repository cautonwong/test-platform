"""
测试层的类型分支必须是注册表驱动的:新增一种设备类型只改"领域 + 驱动 + 清单"
三处,测试层一个字都不用改。这里用"假装收集"的方式把这条约束锁死——
不需要真跑设备,毫秒级跑完。
"""
import importlib.util
from pathlib import Path

import pytest

CONFTEST_PATH = Path(__file__).resolve().parent.parent / "tests" / "conftest.py"
DEVICES_YAML = Path(__file__).resolve().parent.parent / "configs" / "devices.yaml"


def _load_tests_conftest():
    """测试层的 conftest 由 pytest 当插件加载,不经过 normal import,
    这里按文件路径单独加载一份,用来检查它给每个注册类型生成了什么。"""
    spec = importlib.util.spec_from_file_location("tests_conftest_under_test", CONFTEST_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


conftest = _load_tests_conftest()


class _FakeMetafunc:
    """pytest_generate_tests 只用到这三样东西,不需要真的起一个 pytest 会话。"""

    def __init__(self, fixturenames, config_path):
        self.fixturenames = list(fixturenames)
        self.config = self._Config(config_path)
        self.calls = []

    class _Config:
        def __init__(self, path):
            self._path = str(path)

        def getoption(self, name):
            assert name == "--device-config"
            return self._path

    def parametrize(self, argname, values, **kwargs):
        self.calls.append((argname, list(values), kwargs))


def _yaml_ids_of_type(device_type: str) -> list[str]:
    """契约测试只对 role: dut 展开,所以这里也按 manifest.ids_of_type 取。"""
    from testplatform.config.loader import load_device_config
    from testplatform.config.manifest import parse_manifest

    return list(parse_manifest(load_device_config(DEVICES_YAML)).ids_of_type(device_type))


@pytest.mark.parametrize("device_type", conftest.registered_types())
def test_registered_type_gets_a_type_fixture(device_type):
    for name in (device_type, f"{device_type}_id"):
        fixture = getattr(conftest, name, None)
        assert fixture is not None, f"注册类型 {device_type} 没有生成对应的 {name}"
        # pytest 9 把标记改名了,两个名字都认一下
        assert getattr(fixture, "_fixture_function_marker", None) or getattr(
            fixture, "_pytestfixturefunction", None
        ), f"{name} 不是 fixture"


@pytest.mark.parametrize("device_type", conftest.registered_types())
def test_registered_type_is_parametrized_without_editing_conftest(device_type):
    """用例写出 `def test_x(<类型>)` 或 `def test_x(<类型>_id)`,就得按清单展开,
    conftest 里既没有 if 分支也没有 per-type 代码。"""
    for requested in (device_type, f"{device_type}_id"):
        fake = _FakeMetafunc([requested, "composition_root"], DEVICES_YAML)
        conftest.pytest_generate_tests(fake)
        assert fake.calls == [
            (device_type, _yaml_ids_of_type(device_type), {"indirect": True})
        ], f"请求 {requested} 时的参数化不对"


def test_unregistered_type_fails_before_assembly_and_lists_registered_types(tmp_path):
    """清单里引用了没注册的设备类型时,装配前就报错,并把已注册的类型列出来,
    免得只看到一句 KeyError 还要自己去翻代码。"""
    from testplatform.composition.root import CompositionRoot
    from testplatform.drivers.registry import registered_types

    config = tmp_path / "devices.yaml"
    config.write_text(
        "models:\n"
        "  dtsd1352:\n"
        "    type: meter\n"
        "    protocol: vendor_ascii\n"
        "    capabilities: [电压]\n"
        "    point_table:\n"
        "      电压: {address: 0, length: 7, encoding: ascii, unit: V}\n"
        "devices:\n"
        "  - id: meter_99\n"
        "    model: dtsd1352\n"
        "    access: tcp\n"
        "    mode: simulator\n"
        "    connection: {simulated_response: '220.0 5.0'}\n",
        encoding="utf-8",
    )

    with pytest.raises(KeyError) as excinfo:
        CompositionRoot.build(str(config))

    message = str(excinfo.value)
    assert "meter" in message
    for device_type in registered_types():
        assert device_type in message
