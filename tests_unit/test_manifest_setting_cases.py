"""
设置类条款(setting_cases)的装配期校验。

这类条款不写进场景层:清单里一条一行,用例侧只有一个函数(tests/test_setting_cases.py)。
写错一条在装配前就得报出来 —— 一条条款对应一整条现场结论,静默忽略一条就是少测一条条款。
这一层用 parse_manifest 这条纯函数缝就够,不需要装配设备。
"""
import os
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tests_support.nested_pytest import pytest_command

from testplatform.config.manifest import ManualStep, SettingStep, parse_manifest

WRITABLE = {"address": 3, "length": 2, "encoding": "uint", "writable": True}
READ_ONLY = {"address": 2, "length": 2, "encoding": "uint", "scale": 0.1}
# 多字节可写点:BCD 密码 3 字节、BCD 时钟 6 字节、4 字节整。都是写路径要支持的那种点。
PASSWORD = {"address": 8, "length": 3, "encoding": "bcd", "writable": True}
CLOCK = {"address": 4, "length": 6, "encoding": "bcd", "writable": True}
UINT32 = {"address": 10, "length": 4, "encoding": "uint", "writable": True}

# 台架型号:量名沿用上游条款写法,地址还是占位(不在这里验真实地址)。
BENCH = {
    "type": "grid_simulator",
    "protocol": "vendor_ascii",
    "capabilities": ["Ua", "Fre", "接线方式", "Ia"],
    "point_table": {
        "Ua": {"address": "TODO", "length": 7, "encoding": "ascii", "unit": "V", "writable": True},
        "Fre": {"address": "TODO", "length": 7, "encoding": "ascii", "unit": "Hz", "writable": True},
        # 枚举 / 整数参数:编码是 uint 而不是浮点。
        "接线方式": {"address": "TODO", "length": 2, "encoding": "uint", "writable": True},
        # 不可写的量:没有 writable 就不能下发。
        "Ia": {"address": "TODO", "length": 7, "encoding": "ascii", "unit": "A"},
    },
}


def _manifest(setting_cases, models=None):
    return {
        "models": models
        or {
            "可写电表": {
                "type": "meter",
                "protocol": "modbus_rtu",
                "capabilities": ["有功计量模式"],
                "point_table": {"有功计量模式": WRITABLE, "电压": READ_ONLY},
            },
            # 台架型号:只有它在清单里,bench: 前置才有地方下发。
            "三相台架": BENCH,
        },
        "setting_cases": setting_cases,
    }


def _clause(steps=({"write": "有功计量模式", "value": "1601", "expect": "1601"},)):
    return {"clause": "DLT645-4.5.2", "steps": list(steps)}


def _error(setting_cases, models=None) -> str:
    with pytest.raises((ValueError, KeyError)) as excinfo:
        parse_manifest(_manifest(setting_cases, models))
    return str(excinfo.value)


def _bench_error(bench, models=None) -> str:
    """台架前置写错时的那句话:必须点名是哪个量、错在哪里。"""
    return _error(
        {"可写电表": {"M_00_00_13_0002FF_61": {**_clause(), "bench": bench}}},
        models or {
            "可写电表": {
                "type": "meter",
                "protocol": "modbus_rtu",
                "capabilities": ["有功计量模式"],
                "point_table": {"有功计量模式": WRITABLE},
            },
            "三相台架": BENCH,
        },
    )


def test_a_bench_prefix_is_a_field_not_a_step():
    """条款开头那条台架设定写成 bench: 字段:不进 steps(ADR 0006)。

    量名过与导入器同一份 params.yaml,落成台架点表的读数键;它不是被测行为,
    所以 steps 一字不变。
    """
    case = parse_manifest(
        _manifest(
            {"可写电表": {"M_00_00_13_0002FF_61": {**_clause(), "bench": {"Ua": 220, "Fre": 50}}}}
        )
    ).setting_cases["M_00_00_13_0002FF_61"]

    assert dict(case.bench) == {"Ua": 220.0, "Fre": 50.0}
    assert [(step.write, step.value) for step in case.steps] == [("有功计量模式", 1601.0)]


def test_a_clause_without_bench_has_an_empty_prefix():
    """没写 bench: 的条款行为一字不变:前置是空映射,不是 None。"""
    case = parse_manifest(
        _manifest({"可写电表": {"M_00_00_01_000002_18": _clause()}})
    ).setting_cases["M_00_00_01_000002_18"]

    assert case.bench == {}


def test_a_typoed_bench_quantity_is_rejected_with_the_available_names():
    """拼错一个台架量名(θa 写成 ta)在装配前报错:静默设了个没人读的量,报告里看不出来。"""
    message = _bench_error({"ta": 0, "Ua": 220})

    assert "'ta'" in message and "台架量里" in message
    assert "'θa'" in message and "'Ua'" in message, "报错要直接给出可用的量名"


def test_a_bench_quantity_the_bench_model_does_not_have_is_rejected():
    """名字在那份对照表里、但台架型号点表里没有这个量:不能下发,装配前报错。"""
    message = _bench_error({"MeterConst": 100})

    assert "MeterConst" in message and "三相台架" in message and "点表里没有它" in message


def test_a_bench_quantity_that_is_not_writable_is_rejected():
    """台架点没声明 writable:只能读不能设,写进去就是另一件事。"""
    message = _bench_error({"Ia": 5})

    assert "Ia" in message and "writable" in message


@pytest.mark.parametrize("bench", [{"接线方式": "3P4W"}, {"Ua": "220V"}, {"Fre": True}])
def test_a_bench_value_that_is_not_a_number_is_rejected(bench):
    """台架要的是数:枚举名要先在 params.yaml 里落成整数编码,带单位的字串也不是数。"""
    message = _bench_error(bench)

    assert "必须是数值" in message
    assert next(iter(bench)) in message


def test_a_bench_prefix_without_a_bench_model_is_rejected():
    """清单里根本没有台架型号:这些量没地方下发,装配前就说清楚。"""
    message = _error(
        {"可写电表": {"M_00_00_13_0002FF_61": {**_clause(), "bench": {"Ua": 220}}}},
        {
            "可写电表": {
                "type": "meter",
                "protocol": "modbus_rtu",
                "capabilities": ["有功计量模式"],
                "point_table": {"有功计量模式": WRITABLE},
            }
        },
    )

    assert "bench" in message and "grid_simulator" in message


def test_an_empty_bench_mapping_is_rejected():
    """bench: {} 是无意义的写法(要么不写,要么写量):不静默当成没写。"""
    message = _bench_error({})

    assert "bench" in message and "非空映射" in message


def test_one_line_in_the_manifest_becomes_one_parsed_clause():
    """清单里 3–6 行就是一条条款:编号、条款出处、型号、步骤,写作 value 按数值收下。"""
    case = parse_manifest(
        _manifest({"可写电表": {"M_00_00_01_000002_18": _clause()}})
    ).setting_cases["M_00_00_01_000002_18"]

    assert (case.id, case.model, case.clause) == (
        "M_00_00_01_000002_18",
        "可写电表",
        "DLT645-4.5.2",
    )
    assert [(step.write, step.value, step.expect) for step in case.steps] == [
        ("有功计量模式", 1601.0, "1601")
    ]


def test_unknown_step_key_is_rejected_and_lists_the_vocabulary():
    """步骤词表只有 write / value / expect:写成 when / until 这类词直接报错,不猜。"""
    message = _error(
        {"可写电表": {"M_00_00_01_000002_18": _clause([{"write": "有功计量模式", "value": 1, "expect": 1, "when": "先复位"}])}}
    )

    assert "when" in message
    assert "'write'" in message and "'value'" in message and "'expect'" in message


def test_a_missing_step_key_is_rejected():
    message = _error({"可写电表": {"M_00_00_01_000002_18": _clause([{"write": "有功计量模式", "value": 1}])}})

    assert "expect" in message and "缺少" in message


def test_writing_a_point_without_writable_is_rejected_and_names_the_writable_ones():
    """点表没声明可写的点不能写:能写哪些由点表说了算,不由用例猜。"""
    message = _error(
        {"可写电表": {"M_00_00_01_000002_18": _clause([{"write": "电压", "value": 230, "expect": 230}])}}
    )

    assert "电压" in message and "writable" in message
    assert "可写的点: ['有功计量模式']" in message


def test_writing_a_point_the_model_does_not_have_is_rejected():
    message = _error(
        {"可写电表": {"M_00_00_01_000002_18": _clause([{"write": "有功功率", "value": 1, "expect": 1}])}}
    )

    assert "有功功率" in message and "可用的读数键" in message


def test_expect_must_be_one_value_not_a_range_or_a_list():
    """期望是区间就归判据集(equals 那一套),这里只表达单值相等。"""
    message = _error(
        {"可写电表": {"M_00_00_01_000002_18": _clause([{"write": "有功计量模式", "value": 1601, "expect": {"min": 1600, "max": 1600}}])}}
    )

    assert "区间" in message and "列表" in message


def test_value_must_be_a_number_the_write_primitive_can_encode():
    message = _error(
        {"可写电表": {"M_00_00_01_000002_18": _clause([{"write": "有功计量模式", "value": "F001", "expect": "F001"}])}}
    )

    assert "value" in message and "数值" in message and "F001" in message


def test_bcd_password_and_clock_are_accepted_at_their_own_length():
    """value 的限开成"与点表 encoding 相容":BCD 点上写密码 / 时钟形态的十进制串就行。

    长度不是问题:3 字节 BCD 与 6 字节 BCD 同一个写法,补多少位前导零由点的 length 定。
    """
    models = {
        "可写电表": {
            "type": "meter",
            "protocol": "modbus_rtu",
            "capabilities": ["密码"],
            "point_table": {"密码": PASSWORD, "时钟": CLOCK, "有功电量阈值": UINT32},
        }
    }
    case = parse_manifest(
        _manifest(
            {
                "可写电表": {
                    "M_00_00_01_000002_18": _clause(
                        (
                            {"write": "密码", "value": "120000", "expect": "120000"},
                            {"write": "时钟", "value": "202601022359", "expect": "202601022359"},
                            {"write": "有功电量阈值", "value": 4000000000, "expect": 4000000000},
                        )
                    )
                }
            },
            models,
        )
    ).setting_cases["M_00_00_01_000002_18"]

    assert [step.value for step in case.steps] == [120000.0, 202601022359.0, 4000000000.0]


def test_a_value_the_point_cannot_hold_is_rejected_at_assembly_time():
    """2 字节写 70000:装配前就报出范围,不等现场写下去被截成 4464 那个看着合理的值。"""
    message = _error(
        {
            "可写电表": {
                "M_00_00_01_000002_18": _clause(
                    ({"write": "有功计量模式", "value": 70000, "expect": 70000},)
                )
            }
        }
    )

    assert "70000" in message and "0–65535" in message


def test_a_point_whose_encoding_the_write_path_cannot_encode_is_rejected():
    """ascii 文本 token 没有唯一的写形态(补空格?补几个?),写不回去就成不了回读比对。"""
    models = {
        "可写电表": {
            "type": "meter",
            "protocol": "modbus_rtu",
            "capabilities": ["有功功率"],
            "point_table": {"有功功率": {"address": 0, "length": 8, "encoding": "ascii", "writable": True}},
        }
    }
    message = _error(
        {"可写电表": {"M_00_00_01_000002_18": _clause(({"write": "有功功率", "value": "1600", "expect": "1600"},))}},
        models,
    )

    assert "不相容" in message and "ascii" in message


def test_referencing_a_model_that_does_not_exist_is_rejected():
    message = _error({"不存在的型号": {"M_00_00_01_000002_18": _clause()}})

    assert "不存在的型号" in message and "已声明的型号" in message


def test_the_same_clause_number_under_two_models_is_rejected():
    """编号是证据与归因的身份:同一个编号挂两个型号,分不清这条结论是谁的点表得出的。"""
    models = {
        "甲": {
            "type": "meter",
            "protocol": "modbus_rtu",
            "capabilities": ["有功计量模式"],
            "point_table": {"有功计量模式": WRITABLE},
        },
        "乙": {
            "type": "meter",
            "protocol": "modbus_rtu",
            "capabilities": ["有功计量模式"],
            "point_table": {"有功计量模式": WRITABLE},
        },
    }
    message = _error(
        {"甲": {"M_00_00_01_000002_18": _clause()}, "乙": {"M_00_00_01_000002_18": _clause()}},
        models,
    )

    assert "M_00_00_01_000002_18" in message and "重复" in message


# ========================= 人工步骤:人做的、没有机读判据、不许默认判过 =========================

def test_a_manual_step_is_an_action_plus_a_text_confirmation():
    """人工操作一步:做什么(manual)+ 由谁按什么确认(expect 是文字说明,不是机读判据)。

    自动步骤与人工步骤写在同一个 steps 列表里,顺序就是现场做的顺序。
    """
    case = parse_manifest(
        _manifest(
            {
                "可写电表": {
                    "M_00_00_13_000101_01": _clause(
                        [
                            {"write": "有功计量模式", "value": "1601", "expect": "1601"},
                            {
                                "manual": "切换上位机通信模式为 HDLC",
                                "expect": "由操作员确认已切换",
                            },
                        ]
                    )
                }
            }
        )
    ).setting_cases["M_00_00_13_000101_01"]

    assert [type(step) for step in case.steps] == [SettingStep, ManualStep]
    assert [(step.action, step.expect) for step in case.manual_steps] == [
        ("切换上位机通信模式为 HDLC", "由操作员确认已切换")
    ]


def test_a_clause_with_only_automatic_steps_has_no_manual_steps():
    case = parse_manifest(
        _manifest({"可写电表": {"M_00_00_01_000002_18": _clause()}})
    ).setting_cases["M_00_00_01_000002_18"]

    assert case.manual_steps == ()


def test_a_manual_step_that_also_writes_is_rejected():
    """人工操作没有可写的点:写了 value 就不是“人做的”,而“写一半 + 人确认另一半”
    在报告里读不出到底哪半算数。"""
    message = _error(
        {
            "可写电表": {
                "M_00_00_13_000101_01": _clause(
                    [
                        {
                            "manual": "切换上位机通信模式为 HDLC",
                            "expect": "由操作员确认已切换",
                            "value": "HDLC",
                        }
                    ]
                )
            }
        }
    )

    assert "manual 步骤没有可写的点" in message and "['value']" in message


def test_a_manual_step_needs_a_text_expect_not_a_machine_checked_value():
    """一个人工步骤带着机读判据,下一步就会有人把它当自动步骤判过 —— 那就是假通过。"""
    message = _error(
        {
            "可写电表": {
                "M_00_00_13_000101_01": _clause(
                    [{"manual": "切换上位机通信模式为 HDLC", "expect": 1601}]
                )
            }
        }
    )

    assert "文字说明" in message and "1601" in message


def test_a_manual_step_without_expect_is_rejected():
    message = _error(
        {
            "可写电表": {
                "M_00_00_13_000101_01": _clause([{"manual": "切换上位机通信模式为 HDLC"}])
            }
        }
    )

    assert "expect" in message and "缺少" in message


def test_referencing_a_model_that_does_not_exist_is_rejected():
    message = _error({"不存在的型号": {"M_00_00_01_000002_18": _clause()}})

    assert "不存在的型号" in message and "已声明的型号" in message


def test_the_same_clause_number_under_two_models_is_rejected():
    """编号是证据与归因的身份:同一个编号挂两个型号,分不清这条结论是谁的点表得出的。"""
    models = {
        "甲": {
            "type": "meter",
            "protocol": "modbus_rtu",
            "capabilities": ["有功计量模式"],
            "point_table": {"有功计量模式": WRITABLE},
        },
        "乙": {
            "type": "meter",
            "protocol": "modbus_rtu",
            "capabilities": ["有功计量模式"],
            "point_table": {"有功计量模式": WRITABLE},
        },
    }
    message = _error(
        {"甲": {"M_00_00_01_000002_18": _clause()}, "乙": {"M_00_00_01_000002_18": _clause()}},
        models,
    )

    assert "M_00_00_01_000002_18" in message and "重复" in message


def _subprocess_failure(command: list[str], result) -> str:
    """子进程失败时那段断言消息:命令行、退出码、stdout/stderr(各留最后 4000 字)。

    只有路径没有原文的失败查不动 —— 命令行要能照着复现,输出要能看出子进程到底说了什么。
    """
    return (
        f"命令行:{command}\n"
        f"退出码:{result.returncode}\n"
        f"stdout:\n{result.stdout[-4000:]}\nstderr:\n{result.stderr[-4000:]}"
    )


def test_a_manifest_without_setting_cases_skips_instead_of_failing_collection(tmp_path):
    """客户清单 / 规模清单里没有 `setting_cases` 段:那不是错,是"这一刻没有设置类条款"。

    以前的表现是整轮收集直接失败(`--device-config 规模清单` → "no tests ran"),等于
    平台没法对一个没有设置条款的清单跑任何事。现在应当只有这一条节点被显式 skip,
    其它用例照常跑、退出码不变。
    """
    manifest = tmp_path / "devices.yaml"
    manifest.write_text(
        "criteria_profiles: {}\n"
        "models:\n"
        "  m:\n"
        "    type: meter\n"
        "    protocol: vendor_ascii\n"
        "    capabilities: [电压]\n"
        "    point_table:\n"
        "      电压: {address: 1, length: 7, encoding: ascii, unit: V}\n"
        "devices: []\n",
        encoding="utf-8",
    )
    command = pytest_command(
        "-q",
        "-rs",  # 跳过原因只在 -rs 里出现;这条用例验的正是那条原因
        "--device-config",
        str(manifest),
        "tests/test_setting_cases.py",
    )
    result = subprocess.run(
        command,
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    output = result.stdout + result.stderr
    failure = _subprocess_failure(command, result)

    assert result.returncode == 0, failure
    assert "1 skipped" in output, failure
    assert "没有 setting_cases" in output, failure
    assert "NOTSET" not in output, f"不许把空清单变成 NOTSET 参数再炸收集\n{failure}"


def test_a_failed_nested_run_leaves_the_subprocess_text_in_the_message(tmp_path):
    """子进程失败时断言消息里要有命令行、退出码与子进程原文 —— 这条是本票那条红的守卫。

    清单路径不存在时子进程必然非 0 退出;断言消息必须把那一次的命令行与输出留住,
    否则组合闸门下再红一次,下一个看的人还是只有"1 passed"这一句。
    """
    command = pytest_command(
        "-q", "--device-config", str(tmp_path / "不存在.yaml"), "tests/test_setting_cases.py"
    )
    result = subprocess.run(
        command,
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    failure = _subprocess_failure(command, result)

    assert result.returncode != 0, f"清单不存在却被跑通了:{failure}"
    assert "命令行:" in failure and "不存在.yaml" in failure
    assert f"退出码:{result.returncode}" in failure
    assert result.stderr.strip() and result.stderr[-200:] in failure, "子进程的 stderr 原文要留在消息里"
