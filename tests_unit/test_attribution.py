"""
归因入口:读一个证据目录,把结论分成四类 —— 设备不合格 / 测试无效 / 平台错误 / 没有有效结论。

这里用手工构造的证据目录喂进去:归因是证据的投影,所以只要目录里的记录对,
四类就必须各归各位,而且每一条都要能指回具体的证据文件行。

最要紧的两条:
- 台架问题(断链、被跳过)不许算到设备头上;
- "没跑"不等于"通过":跳过 / 筛掉 / 没跑到 / 证据判断不了都是独立一类,
  全绿一轮也必须说清"这一轮到底跑了多少条",不能把 0 条失败说成 0 条用例。
"""
import json
import subprocess
import sys
from pathlib import Path

from testplatform.reporting.attribution import attribute, render, write

REPO = Path(__file__).resolve().parent.parent

CASE = {"id": "M_00_00_01_000005_02", "clause": "GBT37408-5.2.1", "tags": ["一致性", "读数"]}

CRITERIA = {
    "profile": "meter_220v",
    "overridden": [],
    "criteria": [
        {"quantity": "有功电量", "low": None, "low_exclusive": False,
         "high": 1000.0, "high_exclusive": False, "severity": "reject"},
    ],
}


def _round(tmp_path, *, devices=None, scenarios=None, scope=None, status=None, platform_errors=None):
    """造一轮证据目录:设备 JSONL / 场景 / 范围 / 轮次状态 都是显式写进去的。"""
    directory = tmp_path / "20240101T093000"
    directory.mkdir()
    for device_id, records in (devices or {}).items():
        path = directory / f"{device_id}.jsonl"
        path.write_text(
            "".join(json.dumps({"at": "2024-01-01T09:30:00.000", "device": device_id, **rec},
                               ensure_ascii=False) + "\n" for rec in records),
            encoding="utf-8",
        )
    for name, record in (scenarios or {}).items():
        (directory / f"scenario-{name}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    if scope is not None:
        (directory / "run-scope.json").write_text(
            json.dumps(scope, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    if status is not None:
        (directory / "round-status.json").write_text(
            json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    if platform_errors is not None:
        (directory / "platform-errors.jsonl").write_text(
            "".join(json.dumps(rec, ensure_ascii=False) + "\n" for rec in platform_errors),
            encoding="utf-8",
        )
    return directory


def _reading(*, breaches=(), case=CASE, criteria=CRITERIA):
    return {
        "kind": "reading",
        "mode": "simulator",
        "case": case,
        "criteria": criteria,
        "readings": {"有功电量": 1234.5},
        "evaluation": {
            "passed": not any(b["severity"] == "reject" for b in breaches),
            "breaches": list(breaches),
        },
        "events": [],
    }


def _scope(*, collected, deselected=(), skipped=(), failed=(), executed="all"):
    """collected 的每一项是 (nodeid, 用例编号) 或 (nodeid, 用例编号, 落在哪些设备上)。

    设备只有按设备参数化的用例才有 —— 场景用例的参与方在 scenario-*.json 里,不在范围里。
    executed 默认按"跑完一整轮"给(收集减跳过);传 None 表示旧证据目录 —— 那里没有这个字段。
    """
    record = {
        "note": "本文件只记这一轮的范围。",
        "collected": [_collected(*item) for item in collected],
        "deselected": list(deselected),
        "skipped": list(skipped),
        "failed": list(failed),
    }
    if executed is not None:
        skipped_nodeids = {entry["nodeid"] for entry in skipped}
        record["executed"] = (
            [item[0] for item in collected if item[0] not in skipped_nodeids]
            if executed == "all"
            else list(executed)
        )
    return record


def _collected(nodeid, case, devices=None):
    entry = {"nodeid": nodeid, "case": case}
    if devices:
        entry["devices"] = list(devices)
    return entry


def _clean_status():
    return {"verdict": "clean", "note": "本轮没有断链", "link_lost": []}


BREACH = {
    "quantity": "有功电量",
    "value": 1234.5,
    "limit": 1000.0,
    "exceeded_by": 234.5,
    "severity": "reject",
    "text": "有功电量=1234.5 越过 1000.0,超出 234.5",
}

# 等值判据违约在证据层就是这个形状(见 evidence._breach_of):没有边界字段。
EQUALS_BREACH = {
    "kind": "equals",
    "quantity": "有功计量模式",
    "value": 1602.0,
    "expected": 1601,
    "severity": "reject",
    "text": "有功计量模式 期望 1601,实测 1602.0",
}


# ========================= 设备不合格:哪条判据、超出多少、依据哪条用例 =========================

def test_a_breached_criterion_is_a_device_failure_carrying_its_case_and_evidence_line(tmp_path):
    directory = _round(
        tmp_path,
        devices={"meter_01": [_reading(breaches=[BREACH])]},
        scope=_scope(collected=[("tests/test_meter.py::test_x[meter_01]", CASE["id"])]),
        status=_clean_status(),
    )

    attribution = attribute(directory)

    (failure,) = attribution.device_failures
    assert failure["device"] == "meter_01"
    assert (failure["quantity"], failure["value"], failure["limit"]) == ("有功电量", 1234.5, 1000.0)
    assert failure["exceeded_by"] == 234.5
    assert failure["case"]["id"] == CASE["id"] and failure["case"]["clause"] == CASE["clause"]
    assert failure["ref"] == "meter_01.jsonl:1"  # 能指回具体那一行
    assert attribution.verdict == "failed"


def test_a_warn_breach_is_a_soft_signal_not_a_failure(tmp_path):
    """warn 级超出只是软信号:它进不到"设备不合格",否则每台设备都永远不合格。"""
    warned = {**BREACH, "severity": "warn"}
    directory = _round(
        tmp_path,
        devices={"meter_01": [_reading(breaches=[warned])]},
        scope=_scope(collected=[("tests/test_meter.py::test_x[meter_01]", CASE["id"])]),
        status=_clean_status(),
    )

    attribution = attribute(directory)

    assert attribution.device_failures == []
    assert attribution.verdict == "clean"


def test_the_breach_records_fed_to_attribution_are_what_the_evidence_layer_writes():
    """归因的守卫喂的是手写记录:它必须与证据层真写出来的同形 ——
    否则两层又各说各的(这张票要治的就是这件事)。"""
    from testplatform.domain.criteria import Criterion
    from testplatform.reporting.evidence import _breach_of

    assert _breach_of(Criterion("有功电量", high=1000.0).evaluate(1234.5)) == BREACH
    assert _breach_of(Criterion("有功计量模式", equals=1601).evaluate(1602.0)) == EQUALS_BREACH


def test_an_equals_breach_is_recorded_with_expected_instead_of_an_empty_range(tmp_path):
    """等值判据没有边界:归因的记录里不许出现 limit / exceeded_by。
    写成 null / 0.0 会被读成「没有边界、没有越界」,而结论是不合格。"""
    directory = _round(
        tmp_path,
        devices={"meter_01": [_reading(breaches=[EQUALS_BREACH])]},
        scope=_scope(collected=[("tests/test_meter.py::test_x[meter_01]", CASE["id"])]),
        status=_clean_status(),
    )

    attribution = attribute(directory)

    (failure,) = attribution.device_failures
    assert (failure["kind"], failure["quantity"], failure["expected"]) == (
        "equals", "有功计量模式", 1601,
    )
    assert failure["value"] == 1602.0
    assert "limit" not in failure and "exceeded_by" not in failure
    assert attribution.verdict == "failed"

    record = json.loads(write(directory, attribution).read_text(encoding="utf-8"))
    (written,) = record["device_failures"]
    assert "limit" not in written and "exceeded_by" not in written
    assert "limit" not in json.dumps(record, ensure_ascii=False)


def test_a_range_breach_record_is_unchanged_by_the_equals_shape(tmp_path):
    """区间违约的记录逐字节不变:消费方按 limit / exceeded_by 读,字段名、含义、
    连键的顺序都不动。这里钉住整条归因记录的 JSON 序列化。"""
    directory = _round(
        tmp_path,
        devices={"meter_01": [_reading(breaches=[BREACH])]},
        scope=_scope(collected=[("tests/test_meter.py::test_x[meter_01]", CASE["id"])]),
        status=_clean_status(),
    )

    (failure,) = attribute(directory).device_failures

    assert json.dumps(failure, ensure_ascii=False) == (
        '{"kind": "criterion", "device": "meter_01", "case": {"id": "M_00_00_01_000005_02", '
        '"clause": "GBT37408-5.2.1", "tags": ["一致性", "读数"]}, '
        '"quantity": "有功电量", "value": 1234.5, "limit": 1000.0, "exceeded_by": 234.5, '
        '"text": "有功电量=1234.5 越过 1000.0,超出 234.5", "ref": "meter_01.jsonl:1"}'
    )


def test_one_round_holding_both_shapes_keeps_each_record_clean(tmp_path):
    """同一轮里等值违约与区间违约并存:两种形态各自正确,不互相污染 ——
    一个用 expected,一个用 limit / exceeded_by,两边的键不许串。"""
    directory = _round(
        tmp_path,
        devices={
            "meter_01": [_reading(breaches=[BREACH])],
            "meter_02": [_reading(breaches=[EQUALS_BREACH])],
        },
        scope=_scope(
            collected=[
                ("tests/test_meter.py::test_x[meter_01]", CASE["id"]),
                ("tests/test_meter.py::test_y[meter_02]", CASE["id"]),
            ]
        ),
        status=_clean_status(),
    )

    attribution = attribute(directory)

    by_device = {entry["device"]: entry for entry in attribution.device_failures}
    range_entry, equals_entry = by_device["meter_01"], by_device["meter_02"]
    assert (range_entry["kind"], range_entry["limit"], range_entry["exceeded_by"]) == (
        "criterion", 1000.0, 234.5,
    )
    assert "expected" not in range_entry
    assert (equals_entry["kind"], equals_entry["expected"]) == ("equals", 1601)
    assert "limit" not in equals_entry and "exceeded_by" not in equals_entry

    text = "\n".join(render(attribution))
    assert "meter_01  有功电量=1234.5 越过 1000.0,超出 234.5" in text
    assert "meter_02  有功计量模式 期望 1601,实测 1602.0" in text
    assert "limit" not in text and "exceeded_by" not in text, text


def test_an_equals_breach_without_text_still_reads_out_both_numbers(tmp_path):
    """等值违约没有 limit 可以看:证据里万一没带 text,终端也得说清期望多少、实测多少。"""
    silent = {key: value for key, value in EQUALS_BREACH.items() if key != "text"}
    directory = _round(tmp_path, devices={"meter_02": [_reading(breaches=[silent])]})

    text = "\n".join(render(attribute(directory)))

    assert "meter_02  有功计量模式 期望 1601,实测 1602.0" in text


def test_a_scenario_failed_on_a_dut_step_is_that_devices_failure(tmp_path):
    scenario = {
        "scenario": "grid_frequency",
        "case": CASE,
        "verdict": "failed",
        "note": "有被测设备的步骤不成立",
        "participants": [{"id": "grid_sim_01", "role": "support"}, {"id": "inv_01", "role": "dut"}],
        "steps": [
            {"device": "grid_sim_01", "action": "设定电网 50Hz", "passed": True, "detail": {}},
            {"device": "inv_01", "action": "读频率", "passed": False,
             "detail": {"breaches": ["grid_freq_hz=60.0 越过 55"]}},
        ],
        "evidence": {"inv_01": "inv_01.jsonl"},
    }
    directory = _round(tmp_path, scenarios={"grid_frequency": scenario}, status=_clean_status())

    attribution = attribute(directory)

    (failure,) = attribution.device_failures
    assert (failure["kind"], failure["device"], failure["action"]) == (
        "scenario_step", "inv_01", "读频率",
    )
    assert failure["ref"] == "scenario-grid_frequency.json#steps[1]"
    assert attribution.invalid_tests == []


# ========================= 测试无效:台架问题不算到设备头上 =========================

def test_a_support_link_lost_and_its_skipped_case_are_invalid_not_a_device_failure(tmp_path):
    directory = _round(
        tmp_path,
        devices={
            "grid_sim_01": [
                {"kind": "frame", "direction": "send", "hex": "534554"},
                {"kind": "link_lost", "operation": "receive", "failure": "closed",
                 "message": "10.0.0.8:502 对端已关闭连接"},
            ],
            "inv_01": [_reading()],
        },
        scope=_scope(
            collected=[("tests/test_inverter.py::test_x[inv_01]", CASE["id"])],
            skipped=[{
                "nodeid": "tests/test_inverter.py::test_y[inv_01]",
                "case": CASE["id"],
                "reason": "测试无效:周边设备 'grid_sim_01' 收帧阶段断链: 对端已关闭连接",
            }],
        ),
        status={"verdict": "needs-review", "note": "本轮存在断链", "link_lost": []},
    )

    attribution = attribute(directory)

    assert attribution.device_failures == [], "台架断链不许算成被测设备不合格"
    kinds = {(entry["kind"], entry.get("operation")) for entry in attribution.invalid_tests}
    assert kinds == {("link_lost", "receive"), ("skipped_case", "receive")}
    link_lost = next(e for e in attribution.invalid_tests if e["kind"] == "link_lost")
    assert (link_lost["device"], link_lost["failure"]) == ("grid_sim_01", "closed")
    assert link_lost["ref"] == "grid_sim_01.jsonl:2"
    assert attribution.verdict == "invalid"


def test_a_connect_phase_support_fault_says_which_step_broke(tmp_path):
    """装配期周边设备连不上与收帧期断链要分得开:下一步该查线有没有接,还是跑到一半掉的。"""
    directory = _round(
        tmp_path,
        scope=_scope(
            collected=[("tests/test_inverter.py::test_x[inv_01]", CASE["id"])],
            skipped=[{
                "nodeid": "tests/test_inverter.py::test_x[inv_01]",
                "case": CASE["id"],
                "reason": "测试无效:周边设备 'grid_sim_01' 连接失败: [Errno 111] Connection refused",
            }],
        ),
        status=_clean_status(),
    )

    attribution = attribute(directory)

    (invalid,) = attribution.invalid_tests
    assert (invalid["kind"], invalid["operation"]) == ("skipped_case", "connect")
    assert invalid["ref"] == "run-scope.json#skipped[0]"


def test_a_scenario_invalid_on_a_support_step_names_the_support_device(tmp_path):
    scenario = {
        "scenario": "grid_frequency",
        "case": CASE,
        "verdict": "invalid",
        "note": "只有周边设备的步骤不成立",
        "participants": [{"id": "grid_sim_01", "role": "support"}, {"id": "inv_01", "role": "dut"}],
        "steps": [
            {"device": "grid_sim_01", "action": "设定电网 60Hz", "passed": False,
             "detail": {"freq_hz": 45.0}},
            {"device": "inv_01", "action": "读频率", "passed": True, "detail": {}},
        ],
        "evidence": {"inv_01": "inv_01.jsonl"},
    }
    directory = _round(tmp_path, scenarios={"grid_frequency": scenario}, status=_clean_status())

    attribution = attribute(directory)

    (invalid,) = attribution.invalid_tests
    assert invalid["kind"] == "scenario_invalid"
    assert invalid["failed_steps"][0]["device"] == "grid_sim_01"
    assert attribution.device_failures == []


# ========================= 平台错误:不变量被违反,与被测设备无关 =========================

def test_platform_errors_are_their_own_category_and_pre_empt_the_headline(tmp_path):
    directory = _round(
        tmp_path,
        devices={"bat_01": [_reading()]},
        platform_errors=[{
            "at": "2024-01-01T09:30:00.000",
            "kind": "platform_error",
            "device": None,
            "case": CASE,
            "nodeid": "tests/test_battery.py::test_soc[bat_01]",
            "error": "InvariantViolation",
            "message": "SOC 越出 [0, 100]: 120",
        }],
        scope=_scope(collected=[("tests/test_battery.py::test_soc[bat_01]", CASE["id"])]),
        status=_clean_status(),
    )

    attribution = attribute(directory)

    (error,) = attribution.platform_errors
    assert (error["error"], error["nodeid"]) == (
        "InvariantViolation", "tests/test_battery.py::test_soc[bat_01]",
    )
    assert error["ref"] == "platform-errors.jsonl:1"
    assert attribution.device_failures == [], "平台 bug 不能算成设备不合格"
    assert attribution.verdict == "platform-error"


# ========================= 没有有效结论:筛掉 / 跳过 / 没跑到,各是各的 =========================

def test_deselected_and_skipped_cases_are_separate_from_pass(tmp_path):
    directory = _round(
        tmp_path,
        devices={"meter_01": [_reading()]},
        scope=_scope(
            collected=[
                ("tests/test_meter.py::test_x[meter_01]", CASE["id"]),
                ("tests/test_meter.py::test_y[meter_01]", "M_00_00_01_000006_01"),
            ],
            deselected=[{
                "nodeid": "tests/test_meter.py::test_z[meter_01]",
                "case": "M_00_00_01_000007_01",
                "reason": "不满足 --select case:M_00_00_01_000005",
            }],
            skipped=[{
                "nodeid": "tests/test_meter.py::test_cap[meter_01]",
                "case": "M_00_00_01_000008_01",
                "reason": "meter_01 未声明能力 ['无功电量'],跳过",
            }],
        ),
        status=_clean_status(),
    )

    attribution = attribute(directory)

    by_kind = {entry["kind"]: entry for entry in attribution.no_conclusion}
    assert by_kind["deselected"]["case"] == "M_00_00_01_000007_01"
    assert by_kind["deselected"]["ref"] == "run-scope.json#deselected[0]"
    assert by_kind["skipped"]["case"] == "M_00_00_01_000008_01"
    # 收集到了、证据里也没有读数、又不是被跳过/筛掉的:单独一条,名单可追
    not_run = by_kind["not_run"]
    assert [item["nodeid"] for item in not_run["items"]] == [
        "tests/test_meter.py::test_y[meter_01]"
    ]
    assert attribution.verdict == "clean", "没有失败项,但没跑的用例在「没有有效结论」里看得见"


# ========================= 人工步骤:不算通过、不算失败,也不归到设备头上 =========================

def test_a_manual_step_is_no_conclusion_not_a_device_failure(tmp_path):
    """人工步骤不产生读数、也不判合格,但绝不算通过:归到「没有有效结论」,
    并在规模行里单列“待人工确认 N 条”—— 藏在“被跳过”里就等于静默少测。

    停在这一步之前已经跑掉的自动步骤也要说得出来:放在人工步骤前的两条自动步骤全过了,
    skip 不等于一步没跑。"""
    nodeid = "tests/test_setting_cases.py::test_setting_roundtrip[M_00_00_13_000101_01]"
    case_id = "M_00_00_13_000101_01"
    directory = _round(
        tmp_path,
        devices={
            "meter_01": [
                _reading(case={**CASE, "id": case_id}),
                _reading(case={**CASE, "id": case_id}),
                {
                    "kind": "manual_step",
                    "case": {**CASE, "id": case_id},
                    "action": "切换上位机通信模式为 HDLC",
                    "expect": "由操作员确认已切换",
                    "confirmed": None,
                    "executed_steps": 2,
                },
            ]
        },
        scope=_scope(
            collected=[(nodeid, case_id)],
            skipped=[
                {
                    "nodeid": nodeid,
                    "case": case_id,
                    "reason": "含 1 个待人工确认步骤:不算通过也不算失败,由操作员确认后回填",
                }
            ],
            executed=[],
        ),
        status=_clean_status(),
    )

    attribution = attribute(directory)
    text = "\n".join(render(attribution))

    assert attribution.device_failures == [], "人工步骤不是设备不合格的证据"
    assert attribution.invalid_tests == [], "人工步骤不是台架问题,不许写成测试无效"
    assert attribution.evidence_corrupt == [] and attribution.unrecognized == []
    (manual,) = [entry for entry in attribution.no_conclusion if entry["kind"] == "manual_step"]
    assert manual["device"] == "meter_01"
    assert manual["action"] == "切换上位机通信模式为 HDLC"
    assert manual["confirmed"] is None
    assert manual["executed_steps"] == 2 and manual["pending_steps"] == 1
    assert manual["ref"] == "meter_01.jsonl:3"
    assert "待人工确认 1 条" in text
    assert "[没有有效结论] 2 条" in text, "一条人工步骤 + 一条被跳过的用例"
    assert "切换上位机通信模式为 HDLC" in text and "未回填" in text
    assert "已执行 2 个自动步骤且全部通过,还有 1 步待人工确认" in text, text
    # 这一轮确实一条用例都没跑到:不许读成通过。
    assert attribution.verdict == "incomplete", text


def test_a_round_without_scope_says_the_scope_is_missing_instead_of_claiming_a_small_round(tmp_path):
    """少了 run-scope.json 时,被筛掉/被跳过的用例就无从得知 —— 必须明说范围缺失,不能当成没这回事。"""
    directory = _round(tmp_path, devices={"meter_01": [_reading()]}, status=_clean_status())

    attribution = attribute(directory)
    text = "\n".join(render(attribution))

    assert attribution.scope["available"] is False
    assert "范围缺失" in text
    assert any(entry["kind"] == "scope_missing" for entry in attribution.no_conclusion)


def test_a_device_whose_evidence_file_is_empty_is_not_a_pass(tmp_path):
    directory = _round(
        tmp_path,
        devices={"meter_01": [_reading()], "inv_01": []},
        scope=_scope(collected=[("tests/test_meter.py::test_x[meter_01]", CASE["id"])]),
        status=_clean_status(),
    )

    attribution = attribute(directory)

    (empty,) = [entry for entry in attribution.no_conclusion if entry["kind"] == "no_readings"]
    assert empty["device"] == "inv_01"


# ========================= 没有有效结论分两档:范围外只给计数,范围内没读数逐台列 =========================

def test_empty_devices_outside_the_round_scope_are_one_count_line_not_per_device_entries(tmp_path):
    """只跑了几条用例时,没被点到的设备是"不在本轮范围",不是"漏测":逐条列会浸掉真问题。"""
    directory = _round(
        tmp_path,
        devices={"meter_01": [_reading()], "inv_01": [], "bat_01": []},
        scope=_scope(
            collected=[("tests/test_meter.py::test_x[meter_01]", CASE["id"], ["meter_01"])]
        ),
        status=_clean_status(),
    )

    attribution = attribute(directory)
    text = "\n".join(render(attribution))

    assert [e for e in attribution.no_conclusion if e["kind"] == "no_readings"] == []
    assert attribution.out_of_scope == ["bat_01", "inv_01"]
    assert "另有 2 台设备不在本轮范围内" in text
    assert "设备 inv_01" not in text and "设备 bat_01" not in text
    assert attribution.verdict == "clean", "范围外不算失败,顶行结论不变"


def test_an_empty_device_inside_the_round_scope_is_listed_without_claiming_a_miss(tmp_path):
    """设备在范围内、证据里没有读数:逐台列出来(不许并进"范围外"那一行)。

    但只列事实:用例跑了却没读数、与用例本来就不读数,在证据里长得一模一样 ——
    所以措辞不许潦成"漏测"(见下一条)。
    """
    nodeid = "tests/test_inverter.py::test_x[inv_01]"
    directory = _round(
        tmp_path,
        devices={"meter_01": [_reading()], "inv_01": [], "bat_01": []},
        scope=_scope(collected=[(nodeid, CASE["id"], ["inv_01"])]),
        status=_clean_status(),
    )

    attribution = attribute(directory)
    text = "\n".join(render(attribution))

    (empty,) = [e for e in attribution.no_conclusion if e["kind"] == "no_readings"]
    assert empty["device"] == "inv_01" and empty["case_nodeids"] == [nodeid]
    assert attribution.out_of_scope == ["bat_01"], "meter_01 有读数,不算空文件"
    assert "设备 inv_01" in text and nodeid in text


def test_an_empty_reading_file_of_an_executed_case_says_the_case_just_reads_nothing(tmp_path):
    """只跑一条不读数的用例(连上就算完):设备仍逐台列,但话说到底 ——
    这条用例在 executed 里,所以是「用例本来就不读数」,正常;"漏测"/"该测却没测到"不许出现。"""
    nodeid = "tests/test_inverter.py::test_inverter_connects[inv_01]"
    directory = _round(
        tmp_path,
        devices={"meter_01": [_reading()], "inv_01": []},
        scope=_scope(collected=[(nodeid, CASE["id"], ["inv_01"])]),
        status=_clean_status(),
    )

    attribution = attribute(directory)
    text = "\n".join(render(attribution))

    (empty,) = [e for e in attribution.no_conclusion if e["kind"] == "no_readings"]
    assert "本来就不读数" in empty["reason"] and "不是问题" in empty["reason"]
    assert empty["executed_nodeids"] == [nodeid], "它在 executed 里:这一档分得出来"
    assert "分不出" not in text
    assert empty["ref"] == "inv_01.jsonl" and empty["case_nodeids"] == [nodeid]
    assert "漏测" not in text and "该测却没测到" not in text
    assert attribution.verdict == "clean", "没有读数不是失败,顶行结论不变"


def test_a_case_that_died_before_evidence_says_so_instead_of_the_case_reads_nothing(tmp_path):
    """用例进了 executed 却在写证据前崩了:空读数设备的措辞要指回「未落证据的失败」,
    不能和「本来就不读数」共一句 —— 那是两件不同的事,下一步也不同。"""
    nodeid = "tests/test_inverter.py::test_x[inv_01]"
    directory = _round(
        tmp_path,
        devices={"meter_01": [_reading()], "inv_01": []},
        scope=_scope(collected=[(nodeid, CASE["id"], ["inv_01"])], failed=[{"nodeid": nodeid, "case": CASE["id"]}]),
        status=_clean_status(),
    )

    attribution = attribute(directory)

    (empty,) = [e for e in attribution.no_conclusion if e["kind"] == "no_readings"]
    assert "失败在写证据之前" in empty["reason"] and "本来就不读数" not in empty["reason"]
    assert empty["executed_nodeids"] == [nodeid]
    assert [e["nodeid"] for e in attribution.unevidenced_failures] == [nodeid]


def test_devices_whose_cases_did_not_run_are_one_count_line_not_per_device_entries(tmp_path):
    """用例被跳过、没进 executed:这些设备的读数无从判断,但"没跑到"说一遍就够,
    逐台列(规模下 264 行)只会把真问题浸掉。名单仍在 attribution.json 里。"""
    skipped_nodeid = "tests/test_battery.py::test_soc[bat_01]"
    directory = _round(
        tmp_path,
        devices={"meter_01": [_reading()], "bat_01": []},
        scope=_scope(
            collected=[
                ("tests/test_meter.py::test_x[meter_01]", CASE["id"], ["meter_01"]),
                (skipped_nodeid, CASE["id"], ["bat_01"]),
            ],
            skipped=[{"nodeid": skipped_nodeid, "case": CASE["id"], "reason": "bat_01 未声明能力 ['SOC']"}],
        ),
        status=_clean_status(),
    )

    attribution = attribute(directory)
    text = "\n".join(render(attribution))

    assert [e for e in attribution.no_conclusion if e["kind"] == "no_readings"] == []
    assert attribution.not_run_devices == ["bat_01"]
    assert "另有 1 台设备的用例没跑到" in text
    assert "设备 bat_01" not in text, "没跑到不逐台列"
    assert attribution.as_record()["not_run_devices"] == ["bat_01"], "名单不静默"
    assert attribution.verdict == "clean"


def test_a_round_that_ran_nothing_counts_the_devices_once_instead_of_listing_them(tmp_path):
    """一条用例都没执行(全被跳过 / 筛掉):264 台设备逐条列说的全是同一件事。
    收成一行计数,设备名单留在 attribution.json。"""
    skipped_nodeid = "tests_scenario/test_dut_cleanup.py::test_x"
    directory = _round(
        tmp_path,
        devices={"bat_01": [], "bat_02": []},
        scope=_scope(
            collected=[(skipped_nodeid, CASE["id"])],
            skipped=[{"nodeid": skipped_nodeid, "case": CASE["id"], "reason": "清单里没有 meter_01"}],
        ),
        status=_clean_status(),
    )

    attribution = attribute(directory)
    text = "\n".join(render(attribution))

    assert attribution.not_run_devices == ["bat_01", "bat_02"]
    assert [e for e in attribution.no_conclusion if e["kind"] == "no_readings"] == []
    assert "这一轮没有执行任何用例,2 台设备的读数无从判断" in text
    assert "设备 bat_01" not in text and "设备 bat_02" not in text
    assert attribution.verdict == "incomplete", "0 条用例不是全绿"


def test_a_scope_without_device_information_lists_every_empty_file(tmp_path):
    """范围里没有任何设备信息(比如全是直接取设备的集成用例)时不拆档:
    宁可留着噪声,也不能把空读数设备说成"不在本轮范围"。"""
    directory = _round(
        tmp_path,
        devices={"meter_01": [_reading()], "inv_01": []},
        scope=_scope(collected=[("tests_integration/test_energy_system.py::test_x", CASE["id"])]),
        status=_clean_status(),
    )

    attribution = attribute(directory)
    text = "\n".join(render(attribution))

    assert attribution.device_scope is None
    assert attribution.out_of_scope == []
    assert [(e["kind"], e["device"]) for e in attribution.no_conclusion] == [
        ("no_readings", "inv_01")
    ]
    assert "设备范围: 未知" in text


def test_a_round_that_ran_nothing_never_reads_as_passed(tmp_path):
    """证据目录是空的:0 条失败是真的,但那是 0 条用例,不是"全绿"。"""
    directory = _round(tmp_path, status=_clean_status())

    attribution = attribute(directory)
    text = "\n".join(render(attribution))

    assert attribution.verdict == "incomplete"
    assert "没有失败项" in text
    assert "0 条" in text


# ========================= 证据损坏:责任方是写证据这一侧 =========================

def test_a_broken_evidence_line_is_corrupt_not_a_device_failure(tmp_path):
    directory = _round(tmp_path, devices={"meter_01": [_reading()]}, status=_clean_status())
    path = directory / "meter_01.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + "{这不是 JSON\n", encoding="utf-8")

    attribution = attribute(directory)

    (broken,) = attribution.evidence_corrupt
    assert broken["ref"] == "meter_01.jsonl:2"
    assert "不是合法 JSON" in broken["reason"]
    assert attribution.device_failures == []
    assert attribution.verdict == "evidence-corrupt"


def test_an_evidence_gap_is_corruption_and_says_how_many_readings_are_missing(tmp_path):
    """两轮之间的读数增量超出 history 窗口:写入层自己丢了几条,写下的 evidence_gap。

    它归「证据损坏」而不是「设备不合格」与「平台错误」:设备没错、平台也没 bug,
    少条是因为写证据这一侧取不回来。而且必须报出缺几条 —— 缺 5 条与缺 5000 条
    不是同一件事,一条“有损坏”盖过去就又把静默找回来了。
    """
    gap = {
        "kind": "evidence_gap",
        "missing": 5,
        "window": 1000,
        "note": "两轮之间有 1005 条读数,窗口只留最近 1000 条,最旧的 5 条已被裁掉、不在证据里",
    }
    directory = _round(
        tmp_path, devices={"meter_01": [gap, _reading()]}, status=_clean_status()
    )

    attribution = attribute(directory)
    text = "\n".join(render(attribution))

    (broken,) = attribution.evidence_corrupt
    assert broken["missing"] == 5 and broken["window"] == 1000
    assert broken["ref"] == "meter_01.jsonl:1"
    assert attribution.verdict == "evidence-corrupt"
    assert attribution.device_failures == [] and attribution.platform_errors == []
    assert "缺 5 条读数" in text


def test_a_line_missing_required_fields_is_still_corrupt(tmp_path):
    """能解析但缺 device:这行确实不知道属于哪台设备,仍然是“证据坏了”。"""
    directory = _round(tmp_path, devices={"meter_01": [_reading()]}, status=_clean_status())
    path = directory / "meter_01.jsonl"
    path.write_text(
        path.read_text(encoding="utf-8") + json.dumps({"kind": "reading"}) + "\n",
        encoding="utf-8",
    )

    attribution = attribute(directory)

    (broken,) = attribution.evidence_corrupt
    assert broken["ref"] == "meter_01.jsonl:2"
    assert attribution.verdict == "evidence-corrupt"


def test_an_unrecognised_record_kind_is_a_notice_not_corrupt_evidence(tmp_path):
    """证据里多一种记录 ≠ 证据坏了:单独一条提示,不许把整轮判成 evidence-corrupt。

    否则每加一种记录类型都得改归因,而人会去查写入层 —— 排障方向直接跑偏。
    """
    directory = _round(
        tmp_path,
        devices={"meter_01": [{**_reading(), "kind": "device_mode_changed"}]},
        status=_clean_status(),
    )

    attribution = attribute(directory)
    text = "\n".join(render(attribution))

    assert attribution.evidence_corrupt == []
    assert attribution.verdict != "evidence-corrupt"
    (notice,) = attribution.unrecognized
    assert notice["kind"] == "device_mode_changed"
    assert notice["ref"] == "meter_01.jsonl:1"
    assert "未识别" in text and "device_mode_changed" in text


def test_the_records_this_platform_already_writes_are_all_understood(tmp_path):
    """写原语与块读降级的记录(write / batch_fallback)早就在证据里了:归因认得它们,
    不产生读数、不判合格,也不算未识别。"""
    directory = _round(
        tmp_path,
        devices={
            "meter_01": [
                _reading(),
                {"kind": "write", "key": "有功计量模式", "value": 1601.0,
                 "verified": True, "read_back": 1601.0},
                {"kind": "batch_fallback", "address": 0, "exception": 2},
            ]
        },
        status=_clean_status(),
    )

    attribution = attribute(directory)

    assert attribution.unrecognized == []
    assert attribution.evidence_corrupt == []
    assert attribution.verdict == "clean"
    assert attribution.ran["cases"] == 1


# ========================= 全绿一轮:说没有失败项,也说得清跑了多少条 =========================

def test_a_green_round_says_no_failures_and_still_counts_its_cases(tmp_path):
    collected = [
        ("tests/test_meter.py::test_connects[meter_01]", CASE["id"]),
        ("tests/test_meter.py::test_reads[meter_01]", "M_00_00_01_000006_01"),
        ("tests/test_meter.py::test_cap[meter_01]", "M_00_00_01_000008_01"),
    ]
    directory = _round(
        tmp_path,
        devices={"meter_01": [_reading()]},
        scope=_scope(
            collected=collected,
            skipped=[{"nodeid": collected[2][0], "case": collected[2][1],
                      "reason": "meter_01 未声明能力 ['无功电量'],跳过"}],
        ),
        status=_clean_status(),
    )

    attribution = attribute(directory)
    text = "\n".join(render(attribution))

    assert attribution.verdict == "clean"
    assert "没有失败项" in text
    # 执行 3 - 1 = 2 条:不能把"0 条失败"说成"0 条用例"
    assert "执行了 2 条" in text
    assert "收集 3 条" in text and "跳过 1 条" in text
    assert "还没跑到 0 条" in text


def test_an_interrupted_round_reports_only_the_cases_it_executed(tmp_path):
    """`-x` 中途停下:收集 3 条,只执行 1 条。规模行就得说执行了 1 条、还有 2 条没跑到 ——
    收集减跳过会把没执行的算成执行过。"""
    collected = [
        ("tests/test_meter.py::test_reads[meter_01]", CASE["id"]),
        ("tests/test_meter.py::test_reads_voltage[meter_01]", "M_00_00_01_000006_01"),
        ("tests/test_meter.py::test_reads_freq[meter_01]", "M_00_00_01_000006_02"),
    ]
    directory = _round(
        tmp_path,
        devices={"meter_01": [_reading()]},
        scope=_scope(collected=collected, executed=[collected[0][0]]),
        status=_clean_status(),
    )

    attribution = attribute(directory)
    text = "\n".join(render(attribution))

    assert attribution.ran["executed"] == 1
    assert "执行了 1 条" in text and "还没跑到 2 条" in text
    assert "执行了 3 条" not in text, "没执行的用例不许算成执行过"


def test_a_scope_without_executed_says_the_count_is_unknown(tmp_path):
    """旧证据目录(没有 executed):就说"执行条数未知",不拿收集减跳过冒充。"""
    collected = [
        ("tests/test_meter.py::test_reads[meter_01]", CASE["id"]),
        ("tests/test_meter.py::test_reads_voltage[meter_01]", "M_00_00_01_000006_01"),
    ]
    directory = _round(
        tmp_path,
        devices={"meter_01": [_reading()]},
        scope=_scope(collected=collected, executed=None),
        status=_clean_status(),
    )

    attribution = attribute(directory)
    text = "\n".join(render(attribution))

    assert attribution.ran["executed"] is None and attribution.scope["executed"] is None
    assert "执行条数未知" in text
    assert "执行了 " not in text, text
    # 结论照旧:知道证据里有读数就够了,不能因为口径未知把这一轮读成"没跑"。
    assert attribution.verdict == "clean"


def test_attribution_json_is_written_next_to_the_evidence(tmp_path):
    directory = _round(
        tmp_path,
        devices={"meter_01": [_reading(breaches=[BREACH])]},
        scope=_scope(collected=[("tests/test_meter.py::test_x[meter_01]", CASE["id"])]),
        status=_clean_status(),
    )

    path = write(directory, attribute(directory))

    record = json.loads(path.read_text(encoding="utf-8"))
    assert path.name == "attribution.json" and path.parent == directory
    assert record["verdict"] == "failed"
    assert record["device_failures"][0]["ref"] == "meter_01.jsonl:1"
    assert record["invalid_tests"] == [] and record["no_conclusion"] == []


def test_the_cli_prints_the_sections_and_returns_nonzero_when_the_round_is_not_clean(tmp_path):
    directory = _round(
        tmp_path,
        devices={"meter_01": [_reading(breaches=[BREACH])]},
        scope=_scope(collected=[("tests/test_meter.py::test_x[meter_01]", CASE["id"])]),
        status=_clean_status(),
    )

    result = subprocess.run(
        [sys.executable, str(REPO / "tools" / "attribute_round.py"), str(directory)],
        capture_output=True,
        env={"PYTHONPATH": str(REPO / "src"), "PYTHONIOENCODING": "utf-8", "PATH": ""},
        timeout=120,
    )
    output = (result.stdout + result.stderr).decode("utf-8", "replace")

    assert result.returncode == 1, output
    for section in ("[设备不合格]", "[测试无效]", "[平台错误]", "[没有有效结论]", "[这一轮的规模]"):
        assert section in output, output
    assert "meter_01" in output and "有功电量=1234.5 越过 1000.0" in output


# ========================= 未落证据的失败:pytest 红了,归因不许说 clean =========================

def _scope_with_failures(*, collected, failed=(), errored=(), skipped=()):
    """在 _scope 之上补上失败那两栏:它们和 executed 一样,缺键 = 旧证据目录,不知道。"""
    record = _scope(collected=collected, skipped=skipped)
    record["failed"] = list(failed)
    record["errored"] = list(errored)
    return record


def test_a_case_that_failed_before_writing_evidence_is_not_clean(tmp_path):
    """用例在 setup / call 里崩了、一条读数都没写:证据里看不见它,范围里必须看得见。

    这一条是整张票的要害:pytest 退出码非 0,归因却说 clean,正是这个仓库最忌讳的假绿。
    """
    nodeid = "tests/test_inverter.py::test_x[inv_01]"
    directory = _round(
        tmp_path,
        devices={"meter_01": [_reading()]},
        scope=_scope_with_failures(
            collected=[("tests/test_meter.py::test_x[meter_01]", CASE["id"])],
            failed=[{"nodeid": nodeid, "case": "M_00_00_01_000009_02", "reason": "AssertionError: 3000.0 != 3200.0"}],
            errored=[{"nodeid": "tests/test_inverter.py::test_y[inv_01]", "case": None, "reason": "KeyError: 'inv_01'"}],
        ),
        status=_clean_status(),
    )

    attribution = attribute(directory)
    text = "\n".join(render(attribution))

    assert attribution.verdict == "failed-without-evidence", "pytest 红了,归因不许说 clean"
    assert [entry["kind"] for entry in attribution.unevidenced_failures] == ["failed", "errored"]
    assert "没有失败项" not in text
    assert "[未落证据的失败] 2 条" in text
    assert "失败发生在写证据之前" in text
    assert nodeid in text and "AssertionError: 3000.0 != 3200.0" in text
    # 只记范围事实:它既不能被读成设备不合格,也不能被读成测试无效。
    assert attribution.device_failures == [] and attribution.invalid_tests == []


def test_an_old_scope_without_failure_keys_stays_clean(tmp_path):
    """旧证据目录没有 failed / errored 两栏:那是"不知道",不是"没有失败",也不影响别的结论。"""
    directory = _round(
        tmp_path,
        devices={"meter_01": [_reading()]},
        scope=_scope(collected=[("tests/test_meter.py::test_x[meter_01]", CASE["id"])]),
        status=_clean_status(),
    )

    attribution = attribute(directory)

    assert attribution.unevidenced_failures == []
    assert attribution.verdict == "clean"


# ========================= 同因跳过聚合:100 条只差设备 id 的跳过不许占 100 行 =========================

def test_skips_with_one_cause_collapse_to_a_single_line_with_a_count(tmp_path):
    """规模清单下 100 条同因跳过,逐条列会把真正的失败挤出屏幕(实测最长一行 4904 字)。

    显示上聚成一条 +(×N),完整名单仍在 attribution.json 的 no_conclusion 里 —— 一条都不丢。
    """
    devices = [f"inv_{index:03d}" for index in range(100)]
    collected = []
    skipped = []
    for device in devices:
        nodeid = f"tests/test_device_models.py::test_cap[{device}]"
        collected.append((nodeid, "M_00_00_01_000003_19", [device]))
        skipped.append(
            {
                "nodeid": nodeid,
                "case": "M_00_00_01_000003_19",
                "reason": f"{device}(型号 inverter_pv_series)未声明能力 ['无功电量'],跳过",
            }
        )
    directory = _round(
        tmp_path,
        devices={"inv_000": [_reading()], "inv_099": [_reading()]},
        scope=_scope_with_failures(collected=collected, skipped=skipped),
        status=_clean_status(),
    )

    attribution = attribute(directory)
    text = "\n".join(render(attribution))

    skip_lines = [line for line in text.splitlines() if "被跳过" in line]
    assert len(skip_lines) <= 5, "\n".join(skip_lines)
    assert len(skip_lines) == 1 and "(×100)" in skip_lines[0], skip_lines
    assert "无功电量" in skip_lines[0]
    # 完整名单不丢:机器可读那份仍是 100 条,一条不少。
    record = json.loads(write(directory, attribution).read_text(encoding="utf-8"))
    assert len([e for e in record["no_conclusion"] if e["kind"] == "skipped"]) == 100


def test_many_distinct_skip_causes_show_only_the_first_three(tmp_path):
    """原因不同就分开列,但只展开前 3 个:第 4 个起只给计数,名单去 attribution.json 查。"""
    causes = ["未声明能力 ['无功电量'],跳过", "未声明能力 ['电压'],跳过", "模拟器下没有意义", "不在适用矩阵里"]
    collected = []
    skipped = []
    for index, cause in enumerate(causes):
        nodeid = f"tests/test_meter.py::test_cap[meter_0{index}]"
        collected.append((nodeid, f"M_00_00_01_0000{index}_01", [f"meter_0{index}"]))
        skipped.append({"nodeid": nodeid, "case": f"M_00_00_01_0000{index}_01", "reason": cause})
    directory = _round(
        tmp_path,
        devices={"meter_00": [_reading()]},
        scope=_scope_with_failures(collected=collected, skipped=skipped),
        status=_clean_status(),
    )

    text = "\n".join(render(attribute(directory)))

    skip_lines = [line for line in text.splitlines() if "被跳过" in line]
    assert len(skip_lines) == 3, skip_lines
    assert "另有 1 个不同原因的跳过,共 1 条" in text
    assert "不在适用矩阵里" not in text, "第 4 个原因只计数,不展开"


def test_round_note_names_both_when_a_round_broke_and_failed_to_restore():
    """断链与台架复位失败同时发生:文案必须两个都说出来。

    只写"断链"会把排障方向带偏(去看通信,而真正的问题是台架没回到初始设定);
    只写"复位失败"会漏掉真正断掉的那条链路。这一支以前没有用例,坏掉不会有人发现。
    """
    from testplatform.reporting.evidence import SETPOINT_RESTORE_OPERATION, _round_note

    note = _round_note(
        [
            {"device": "grid_sim_01", "operation": SETPOINT_RESTORE_OPERATION},
            {"device": "meter_01", "operation": "receive"},
        ]
    )

    assert "断链与台架复位失败" in note
    assert "测试无效" in note and "不是设备不合格" in note
