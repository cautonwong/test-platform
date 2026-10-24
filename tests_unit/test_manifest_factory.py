"""清单工厂自己的契约:每次交出来的都是新造的一份。

坏的失败模式:有人 `mutate` 改坏型号之后,后面所有用例看到的是被改过的点表,红绿取决于
跑顺序 —— `--collect-only` 的总数和单文件跑绿都发现不了。守卫按"段"遍历:工厂多出一个段
(比如以后加 `test_matrix`)时先红,逼着把新段也纳入隔离检查。
"""
import pytest

from tests_support.manifest_factory import dlt645_bench, manifest

BREAK = "掰坏"

# 每个段挑一处可写的:型号(点表字段 / 协议)、判据集、设备(连接 / 设备参数)。
PATHS = [
    ("models", "inverter_pv_series", "point_table", "电压", "length"),
    ("models", "inverter_pv_series", "protocol"),
    ("criteria_profiles", "grid_50hz"),
    ("devices", 0, "connection", "simulated_response"),
    ("devices", 0, "rated_power_w"),
]


def _at(manifest_dict, path):
    value = manifest_dict
    for step in path:
        value = value[step]
    return value


def _set(manifest_dict, path, value):
    _at(manifest_dict, path[:-1])[path[-1]] = value


def _full_manifest():
    # 三个段都出现(型号 / 判据集 / 周边设备),守卫才有东西可查。
    return manifest(inverters=1, meters=1, support=True)


@pytest.mark.parametrize("path", PATHS, ids=[".".join(map(str, path)) for path in PATHS])
def test_changing_one_manifest_does_not_leak_into_the_next(path):
    expected = _at(_full_manifest(), path)

    broken = _full_manifest()
    _set(broken, path, BREAK)
    assert _at(broken, path) == BREAK

    assert _at(_full_manifest(), path) == expected


def test_every_segment_of_the_factory_output_is_covered_by_this_guard():
    """工厂加一个新段就红:新段没人验过隔离,就别让它悄悄漏 deepcopy。"""
    assert set(_full_manifest()) == {path[0] for path in PATHS}


def test_mutate_sees_the_copy_not_the_module_constant():
    """`mutate` 是"只改一处"的唯一入口:它改到的那一份不能是模块常量。"""
    mutated = manifest(
        inverters=1,
        mutate=lambda m: m["models"]["inverter_pv_series"].update(protocol="dlt645"),
    )

    assert mutated["models"]["inverter_pv_series"]["protocol"] == "dlt645"
    assert manifest(inverters=1)["models"]["inverter_pv_series"]["protocol"] == "vendor_ascii"


def test_dlt645_bench_is_also_freshly_built():
    """`energy=False` 会从型号里摘掉一个点:摘的必须是那一份副本,不是模块常量。"""
    lean = dlt645_bench({"000000000001": {}}, energy=False)
    assert "正向有功总电能" not in lean["models"]["dlt645_meter"]["point_table"]

    assert "正向有功总电能" in dlt645_bench({"000000000001": {}})["models"]["dlt645_meter"]["point_table"]
