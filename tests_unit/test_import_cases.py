"""导入器(tools/import_cases.py)的最小回归:解析切片、对照表校验、三档分类、试点可重复。

导入器是 tools/ 下的脚本,不进 testpaths;这里的测试按文件路径加载它,不 import 平台内部。
只有真读 xlsx 那一条要 openpyxl —— 没装它的解释器上 skip,其余测试照跑。
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "tools" / "import_cases.py"
SHEET = "13通信"

# 试点型号 dtsd1352_modbus 唯一的可写点。同名的 dut 条目能直接映射,不用同义声明。
WRITABLE_KEY = "有功计量模式"

GOOD_PARAMS = f"""
{WRITABLE_KEY}:
  device: dut
  key: {WRITABLE_KEY}
  values:
    "1600": 1600
    "1601": 1601
# 台架量:模拟量直接读字面数,枚举走 values(名字 → 整数编码)。接线方式的编码还没拿到(null)。
Ua: {{device: bench, quantity: Ua, unit: V}}
Fre: {{device: bench, quantity: Fre, unit: Hz}}
接线方式:
  device: bench
  quantity: 接线方式
  values:
    3P4W: 1
    3P3W: null
"""


def _tool():
    spec = importlib.util.spec_from_file_location("import_cases_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool = _tool()


@pytest.fixture(scope="module")
def tables(tmp_path_factory):
    """一份只留试点型号可写点的 params.yaml:测试不靠仓库里恰好有哪条 dut 映射。"""
    directory = tmp_path_factory.mktemp("import")
    (directory / "params.yaml").write_text(GOOD_PARAMS, encoding="utf-8")
    _copy_tables(directory)
    return tool.load_tables(directory, model="dtsd1352_modbus")


def test_parses_the_pilot_sheet():
    """列位置来自表头标签,行号原样保留;合并单元格吃掉的"对象参数"标签不改变步骤归属。

    第 600-602 行是那个坑:步骤2 的 操作类型=写/判断,下一行 "通信模式=HDLC" 没有
    对象参数 标签(被合并单元格吃掉)。按"沿用上一行子标签"处理会把它记成操作类型。
    """
    pytest.importorskip("openpyxl", reason="读 xlsx 要 openpyxl(工具用装了它的解释器跑)")
    sheet = tool.read_workbook(tool.XLSX, [SHEET])[0]

    assert sheet["columns"] == {"label": 2, "title": 3, "sublabel": 3, "value": 4, "expected": 5}
    assert len(sheet["cases"]) == 105
    assert sheet["cases"][0]["case_id"] == "M_00_00_13_000101_01"
    assert sheet["cases"][0]["row"] == 2
    assert sheet["cases"][0]["steps"][1]["row"] == 23

    case = next(case for case in sheet["cases"] if case["case_id"] == "M_00_00_13_000101_11")
    step = next(step for step in case["steps"] if step["index"] == "步骤2")
    assert step["op"] == "写/判断"
    assert step["params"] == ["通信模式=HDLC"]
    assert step["expected"] == "读写一致为合格"


def test_the_missing_openpyxl_hint_names_the_current_interpreter(monkeypatch):
    """建议命令用当前解释器(sys.executable)。写死开发机 venv 路径的话,别人照抄会跑错解释器,
    而且不会报错 —— 它只是一行打印出来的字符串。"""

    class _BlockOpenpyxl:
        def find_spec(self, name, path=None, target=None):
            if name == "openpyxl":
                raise ModuleNotFoundError(f"No module named {name!r}", name=name)

    monkeypatch.setattr(sys, "meta_path", [_BlockOpenpyxl(), *sys.meta_path])
    # 全量跑时别的用例可能已经 import 过 openpyxl,sys.modules 里有缓存就绕过了 meta_path,
    # 于是这条只在全量跑时红(单跑绿)。一并把缓存摘掉,跑完由 monkeypatch 还原。
    monkeypatch.delitem(sys.modules, "openpyxl", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        tool.read_workbook(tool.XLSX, None)

    message = str(excinfo.value)
    assert f"{sys.executable} tools/import_cases.py" in message
    assert "test_platform/.venv" not in message


def test_params_table_rejects_typos_and_unwritable_points(tmp_path):
    """拼错一个键、映射到不可写的点:导入时直接报错,不留到生成时静默跳过。"""
    _copy_tables(tmp_path)

    _params(tmp_path, GOOD_PARAMS.replace("  key:", "  keys:"))
    with pytest.raises(KeyError, match="keys"):
        tool.load_tables(tmp_path, model="dtsd1352_modbus")

    _params(tmp_path, GOOD_PARAMS.replace(f"key: {WRITABLE_KEY}", "key: 电压"))
    with pytest.raises(KeyError, match="电压"):
        tool.load_tables(tmp_path, model="dtsd1352_modbus")


def test_params_table_rejects_a_mapping_without_declared_synonym(tmp_path):
    """源参数名与点表键不同名、又没有 synonyms:导入直接报错。

    这条防的是把"红外通信波特率"指到"有功计量模式"上 —— 写进去再读回来当然一致,
    验的却不是被测条款。名字不同就是另一个量,除非有人显式声明。
    """
    _copy_tables(tmp_path)
    mismatched = GOOD_PARAMS.replace(WRITABLE_KEY, "红外通信波特率", 1)

    _params(tmp_path, mismatched)
    with pytest.raises(KeyError, match="synonyms"):
        tool.load_tables(tmp_path, model="dtsd1352_modbus")

    _params(tmp_path, mismatched.replace(
        f"  key: {WRITABLE_KEY}\n", f"  key: {WRITABLE_KEY}\n  synonyms: [红外通信波特率]\n"))
    loaded = tool.load_tables(tmp_path, model="dtsd1352_modbus")
    assert loaded["params"]["红外通信波特率"]["key"] == WRITABLE_KEY


def _params(tmp_path: Path, text: str) -> None:
    (tmp_path / "params.yaml").write_text(text, encoding="utf-8")


def _copy_tables(directory: Path) -> None:
    """ops.yaml / expects.yaml / symbols.yaml 用仓库里的原样。"""
    source = REPO / "configs" / "import"
    for name in ("ops.yaml", "expects.yaml", "symbols.yaml"):
        (directory / name).write_text((source / name).read_text(encoding="utf-8"), encoding="utf-8")


def _import_dir(directory: Path) -> Path:
    """一份完整的对照表副本(四份都在):换掉清单之后再 load_tables 用。"""
    _copy_tables(directory)
    _params(directory, GOOD_PARAMS)
    return directory


def _step(op, params, expected=None, row=10, index="步骤1"):
    return {"index": index, "row": row, "description": None, "op": op,
            "params": params, "expected": expected}


def _case(case_id, steps, row=2):
    return {"case_id": case_id, "title": f"{case_id} 试点", "row": row, "steps": steps}


def test_coverage_splits_complete_partial_and_blocked(tables):
    """三档:完整(步骤全落得进 setting_cases)/ 部分(有步骤落不进去)/ 不可生成(对照表缺)。"""
    sheet = {"name": SHEET, "cases": [
        # 完整:只有写步骤,参数名与取值都在对照表里,预期结果是等值
        _case("M_00_00_13_000900_01", [
            _step("写/判断", [f"{WRITABLE_KEY}=1601"], "读写一致为合格", row=3),
        ]),
        # 部分:含读步骤(非台架因果链)—— setting_cases 表达不了,也不归场景层
        _case("M_00_00_13_000900_02", [
            _step("写/判断", [f"{WRITABLE_KEY}=1601"], "读写一致为合格", row=5),
            _step("读/记录", [WRITABLE_KEY], row=6),
        ], row=4),
        # 不可生成:参数名不在 params.yaml
        _case("M_00_00_13_000900_03", [
            _step("写/判断", ["通信模式=E模式"], "读写一致为合格", row=8),
        ], row=7),
    ]}

    report = tool.coverage_report(sheet, tables)

    assert [case["case_id"] for case in report["complete"]] == ["M_00_00_13_000900_01"]
    assert [(case["case_id"], dropped) for case, dropped in report["partial"]] == [
        ("M_00_00_13_000900_02", {"read": 1})
    ]
    assert [case["case_id"] for case, _ in report["gaps"]] == ["M_00_00_13_000900_03"]


def test_a_static_bench_prefix_lands_in_the_setting_case(tables):
    """只写/人工 + 一段静态台架前置 → 进 setting_cases,前置写成 bench:{读数键: 值}。

    bench: 不包在 steps 里(ADR 0006):它不是被验的行为,而是让表能测的前提。
    """
    sheet = {"name": SHEET, "cases": [
        _case("M_00_00_13_000900_01", [
            _step("调整台体", ["Ua=220", "Fre=50"], row=3),
            _step("写/判断", [f"{WRITABLE_KEY}=1601"], "读写一致为合格", row=4),
        ]),
    ]}

    report = tool.coverage_report(sheet, tables)
    pilot = tool.pilot_cases([sheet], tables, limit=10)

    assert [case["case_id"] for case in report["complete"]] == ["M_00_00_13_000900_01"]
    assert report["bench"] == {"M_00_00_13_000900_01": {"Ua": 220.0, "Fre": 50.0}}
    assert report["routes"] == [
        (sheet["cases"][0], "consistency", "只有写 / 人工步骤 + 台架前置(步骤1)")
    ]
    emitted = yaml.safe_load(tool.render_pilot(pilot, tables, [sheet]))
    clause = emitted["setting_cases"][tables["model"]]["M_00_00_13_000900_01"]
    assert clause["bench"] == {"Ua": 220.0, "Fre": 50.0}
    assert clause["steps"] == [{"write": WRITABLE_KEY, "value": 1601, "expect": 1601}]


def test_an_observed_bench_action_routes_the_clause_to_the_scenario_layer(tables):
    """把台架调到 X → 读表 → 判 Y 有因果链:整条归场景层,不计入可生成数(ADR 0006)。

    判定必须显式:报表里每条条款都带着归属与理由,而不是静默少算一条。
    """
    sheet = {"name": SHEET, "cases": [
        _case("M_00_00_13_000900_01", [
            _step("调整台体", ["Ua=220"], row=3),
            _step("读/判断", [WRITABLE_KEY], "读取成功为合格", row=4),
        ]),
        # 中途两次调台体:台架设定本身是被测行为
        _case("M_00_00_13_000900_02", [
            _step("调整台体", ["Ua=220"], row=6),
            _step("调整台体", ["Ua=0"], row=7),
        ], row=5),
    ]}

    report = tool.coverage_report(sheet, tables)

    assert report["complete"] == []
    assert [(case["case_id"], reason) for case, reason in report["scenario"]] == [
        ("M_00_00_13_000900_01", "台架设定被观测(步骤1 跟在台架动作后面)"),
        ("M_00_00_13_000900_02", "条款中途多次调台体(步骤1, 步骤1)"),
    ]
    rendered = tool.render_coverage([sheet], [report], tables)
    assert "归场景层(不计入可生成数) 2" in rendered
    assert "- M_00_00_13_000900_01: scenario —— 台架设定被观测" in rendered


def test_an_unknown_bench_value_is_a_mapping_gap_not_a_zero(tables):
    """接线方式的未知取值 / 未定编码在导入侧报错:名字不在 values 里、值还是 null 都进缺口。

    拿 0 顶和编一个寄存器地址是同一种假绿 —— 台架会收到一个看起来像编码的数。
    """
    sheet = {"name": SHEET, "cases": [
        _case("M_00_00_13_000900_01", [
            _step("调整台体", ["接线方式=3P3W"], row=3),
        ]),
        _case("M_00_00_13_000900_02", [
            _step("调整台体", ["接线方式=单相"], row=5),
        ], row=4),
        # 符号值:源文件写 Ux+5V / Ib,两个都不是数,也不在 symbols.yaml 里
        _case("M_00_00_13_000900_03", [
            _step("调整台体", ["Ua=Ux+5V"], row=7),
        ], row=6),
    ]}

    report = tool.coverage_report(sheet, tables)
    gaps = {case["case_id"]: detail for case, detail in report["gaps"]}

    assert report["complete"] == []
    assert gaps["M_00_00_13_000900_01"] == [
        "bench_enum: 步骤1@3 '接线方式=3P3W' 整数编码未定(params.yaml 里这一档还是 null,不拿 0 顶)"
    ]
    assert "未知取值(可用: ['3P3W', '3P4W'])" in gaps["M_00_00_13_000900_02"][0]
    assert gaps["M_00_00_13_000900_03"][0].startswith("bench_symbol: 步骤1@7 'Ua=Ux+5V'")
    assert "不是数值" in gaps["M_00_00_13_000900_03"][0]


def test_a_symbol_rejects_when_the_model_has_no_such_device_param(tmp_path, monkeypatch):
    """符号 Un 的拒收消息要能直接改成动作:点名型号、点名缺哪个参数、说清加在哪。

    不拿 220 兜底:220 与 3×(220/380) 是两台不同被试品,猜错的是台架电压 ——
    而那种错跑起来一切正常。这是"型号没声明",与"声明了但值还是 TODO"是两句不同的话。
    """
    manifest = tmp_path / "devices.yaml"
    manifest.write_text(
        "models:\n"
        "  dtsd1352_modbus:\n"
        "    type: meter\n"
        "    point_table:\n"
        f"      {WRITABLE_KEY}: {{address: 3, length: 2, encoding: uint, writable: true}}\n"
        "devices:\n"
        "  - {id: meter_01, model: dtsd1352_modbus}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(tool, "DEVICES", manifest)
    resolved = tool.load_tables(_import_dir(tmp_path), model="dtsd1352_modbus")

    sheet = {"name": SHEET, "cases": [
        _case("M_00_00_13_000900_01", [
            _step("调整台体", ["Ua=Un", "Fre=Hz"], row=3),
        ]),
    ]}

    report = tool.coverage_report(sheet, resolved)
    message = report["gaps"][0][1][0]

    assert report["gap_kinds"] == {"bench_model_param": 2}
    assert message.startswith("bench_model_param: 步骤1@3 'Ua=Un'")
    assert "型号 'dtsd1352_modbus' 没有声明 rated_phase_voltage_v" in message
    assert "无法解析符号 'Un'" in message
    assert "device_params 里加上它" in message and "设备实例上给值" in message
    assert "示例: - {id: meter_01, model: dtsd1352_modbus, rated_phase_voltage_v: <铭牌上的数>}" in message
    assert "TODO" not in message  # 没声明 ≠ 值没填,混成一句会让人去改错地方
    assert "220" not in message  # 消息里不出现一个能被照抄的默认额定值


def test_a_TODO_placeholder_is_rejected_instead_of_becoming_a_number(tables):
    """实例上写 TODO = "铭牌值还没填":拒收,不许把占位当成一个数、也不许退回默认值。

    用的是仓库里那份清单:型号声明了三个额定参数、`meter_01` 全是 TODO —— 这条用例把
    "声明了但没填"钉在导入侧,谁把 TODO 换成一个数字都要先过这里。
    """
    sheet = {"name": SHEET, "cases": [
        _case("M_00_00_13_000900_01", [_step("调整台体", ["Ua=Un"], row=3)]),
    ]}

    report = tool.coverage_report(sheet, tables)
    message = report["gaps"][0][1][0]

    assert report["gap_kinds"] == {"bench_model_param": 1}
    assert report["bench"] == {}
    assert "型号 'dtsd1352_modbus' 缺参数 rated_phase_voltage_v" in message
    assert "还是 TODO 占位" in message and "无法解析符号 'Un'" in message
    assert "示例: - {id: meter_01, model: dtsd1352_modbus, rated_phase_voltage_v: <铭牌上的数>}" in message
    assert "220" not in message


def test_the_factory_tables_pin_the_two_naming_decisions():
    """Un 是相电压、Ib 不映射:两条语义决定钉在配置上,不靠注释口口相传。

    Ib 并进 rated_current_a 会把 DL/T 645 的 Ib 与 IEC 的 In 之间那一步换算丢掉;
    确认是基本电流之后另加 basic_current_a 一行(参数名写在 symbols.yaml 的注释里)。
    """
    symbols_text = (REPO / "configs" / "import" / "symbols.yaml").read_text(encoding="utf-8")
    symbols = yaml.safe_load(symbols_text)
    manifest = yaml.safe_load((REPO / "configs" / "devices.yaml").read_text(encoding="utf-8"))

    assert symbols["Un"]["param"] == "rated_phase_voltage_v"
    assert symbols["In"]["param"] == "rated_current_a"
    assert "Ib" not in symbols
    assert "basic_current_a" in symbols_text
    assert set(manifest["models"]["dtsd1352_modbus"]["device_params"]) == {
        "rated_phase_voltage_v", "rated_current_a", "rated_freq_hz"
    }


def test_a_symbol_resolves_to_the_device_param_given_on_the_instance(tables, tmp_path, monkeypatch):
    """符号 → 型号参数 → 设备实例上的值。型号声明了、实例给了值,台架前置才落得成数。"""
    manifest = tmp_path / "devices.yaml"
    manifest.write_text(
        "models:\n"
        "  dtsd1352_modbus:\n"
        "    type: meter\n"
        "    device_params: [rated_phase_voltage_v]\n"
        "    point_table:\n"
        f"      {WRITABLE_KEY}: {{address: 3, length: 2, encoding: uint, writable: true}}\n"
        "devices:\n"
        "  - {id: meter_01, model: dtsd1352_modbus, rated_phase_voltage_v: 230}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(tool, "DEVICES", manifest)
    resolved = tool.load_tables(_import_dir(tmp_path), model="dtsd1352_modbus")

    sheet = {"name": SHEET, "cases": [
        _case("M_00_00_13_000900_01", [
            _step("调整台体", ["Ua=Un"], row=3),
            _step("写/判断", [f"{WRITABLE_KEY}=1601"], "读写一致为合格", row=4),
        ]),
        # Hz 在同一型号上仍然欠着 rated_freq_hz:一档一个原因,不互相掩盖
        _case("M_00_00_13_000900_02", [
            _step("调整台体", ["Fre=Hz"], row=6),
        ], row=5),
    ]}
    report = tool.coverage_report(sheet, resolved)

    assert report["bench"] == {"M_00_00_13_000900_01": {"Ua": 230.0}}
    assert [case["case_id"] for case in report["complete"]] == ["M_00_00_13_000900_01"]
    assert report["gap_kinds"] == {"bench_model_param": 1}


def test_a_symbol_rejects_when_two_instances_disagree(tables, tmp_path, monkeypatch):
    """同型号两台实例给的额定电压不一样:条款里只有一列值,挑第一台就是编。"""
    manifest = tmp_path / "devices.yaml"
    manifest.write_text(
        "models:\n"
        "  dtsd1352_modbus:\n"
        "    type: meter\n"
        "    device_params: [rated_phase_voltage_v]\n"
        "    point_table:\n"
        f"      {WRITABLE_KEY}: {{address: 3, length: 2, encoding: uint, writable: true}}\n"
        "devices:\n"
        "  - {id: meter_01, model: dtsd1352_modbus, rated_phase_voltage_v: 220}\n"
        "  - {id: meter_02, model: dtsd1352_modbus, rated_phase_voltage_v: 380}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(tool, "DEVICES", manifest)
    resolved = tool.load_tables(_import_dir(tmp_path), model="dtsd1352_modbus")

    sheet = {"name": SHEET, "cases": [
        _case("M_00_00_13_000900_01", [_step("调整台体", ["Ua=Un"], row=3)]),
    ]}
    message = tool.coverage_report(sheet, resolved)["gaps"][0][1][0]

    assert "取了不同的值" in message and "统一额定值" in message


def test_symbols_table_rejects_a_fallback_or_a_missing_source(tmp_path):
    """符号表没有 fallback 这一档:写了就在导入时报错,不许"没有参数时用 220"。"""
    _copy_tables(tmp_path)
    _params(tmp_path, GOOD_PARAMS)
    symbols = tmp_path / "symbols.yaml"

    symbols.write_text(
        "Un: {param: rated_phase_voltage_v, fallback: 220, source: \"tests/param.py:13\", expect: \"Un = 230\"}\n",
        encoding="utf-8",
    )
    with pytest.raises(KeyError, match="fallback"):
        tool.load_tables(tmp_path, model="dtsd1352_modbus")

    symbols.write_text("Un: {param: rated_phase_voltage_v, expect: 'Un = 230'}\n", encoding="utf-8")
    with pytest.raises(KeyError, match="source"):
        tool.load_tables(tmp_path, model="dtsd1352_modbus")

    symbols.write_text(
        "Un: {param: rated_phase_voltage_v, source: 'tests/param.py', expect: 'Un = 230'}\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="<相对路径>:<行号>"):
        tool.load_tables(tmp_path, model="dtsd1352_modbus")


def test_the_report_shows_the_missing_device_param_tier_separately(tables):
    """报表把"缺型号参数"单列一档,不和"符号值认不出"混成一句"映射缺口"。"""
    sheet = {"name": SHEET, "cases": [
        _case("M_00_00_13_000900_01", [
            _step("调整台体", ["Ua=Un", "Fre=Ux"], row=3),
        ]),
    ]}

    rendered = tool.render_coverage([sheet], [tool.coverage_report(sheet, tables)], tables)

    assert "缺型号参数 1 处" in rendered
    assert "符号值认不出 1 处" in rendered
    assert "#### 缺型号参数:" in rendered and "#### 符号值认不出:" in rendered
    assert "缺参数 rated_phase_voltage_v" in rendered


def test_pilot_takes_only_complete_cases(tables):
    """部分条款不进试点:少了一半步骤也绿的用例,读报告的人分不出它验了半个还是全个。"""
    sheet = {"name": SHEET, "cases": [
        _case("M_00_00_13_000900_01", [
            _step("写/判断", [f"{WRITABLE_KEY}=1601"], "读写一致为合格", row=3),
            _step("写/判断", [f"{WRITABLE_KEY}=1600"], "读写一致为合格", row=4),
        ]),
        # 台架前置要落数:落不进去的台架取值同样是缺口(符号值 / 未定编码),不能半条上线
        _case("M_00_00_13_000900_02", [
            _step("调整台体", ["Ua=Un"], row=5),
            _step("写/判断", [f"{WRITABLE_KEY}=1601"], "读写一致为合格", row=6),
        ], row=4),
        # 含人工步骤:跑起来是 skip,不算试绿
        _case("M_00_00_13_000900_03", [
            _step("人工操作", ["切换上位机"], row=8),
        ], row=7),
    ]}

    first = tool.pilot_cases([sheet], tables, limit=10)
    second = tool.pilot_cases([sheet], tables, limit=10)

    assert first == second
    assert [case["case_id"] for case in first["cases"]] == ["M_00_00_13_000900_01"]
    assert first["cases"][0]["steps"] == [
        {"write": WRITABLE_KEY, "value": 1601, "expect": 1601},
        {"write": WRITABLE_KEY, "value": 1600, "expect": 1600},
    ]
    assert first["cases"][0]["bench"] == {}
    assert first["tiers"] == [{"sheet": SHEET, "total": 3, "complete": 1, "scenario": 0,
                               "partial": 1, "gaps": 1, "structural": 0}]


def test_empty_pilot_says_so_instead_of_going_quiet(tables):
    """一条完整的都没有时,清单头与报表都把三档数写出来:0 条是结论,不是没跑。"""
    sheet = {"name": SHEET, "cases": [
        _case("M_00_00_13_000900_01", [
            _step("写/判断", ["通信模式=E模式"], "读写一致为合格", row=3),
        ]),
    ]}

    pilot = tool.pilot_cases([sheet], tables, limit=10)
    rendered = tool.render_pilot(pilot, tables, source_workbook=[sheet])
    coverage = tool.render_coverage([sheet], [tool.coverage_report(sheet, tables)], tables, pilot)

    assert pilot["cases"] == []
    assert pilot["write_gaps"] == [("通信模式", 1)]
    assert "本次 0 条" in rendered and "完整 0" in rendered
    assert "通信模式" in coverage  # 缺什么名字,报表点名
    manifest = yaml.safe_load(tool.render_pilot_manifest(pilot))
    # 空试点不建 `型号: {}`:清单装配拒收空映射,一份 0 条的清单会连加载都过不了
    assert all(clauses for clauses in manifest["setting_cases"].values())


def test_rendered_artifacts_are_byte_identical_across_runs(tables):
    """cases.json、试点清单、合并后的设备清单:同一份输入渲染两次逐字节相同。"""
    sheet = {"name": SHEET, "cases": [
        _case("M_00_00_13_000900_01", [
            _step("写/判断", [f"{WRITABLE_KEY}=1600"], "读写一致为合格"),
        ]),
    ]}
    pilot = tool.pilot_cases([sheet], tables, limit=10)

    for render in (
        lambda: json.dumps({"sheets": [sheet]}, ensure_ascii=False, indent=2, sort_keys=True),
        lambda: tool.render_pilot(pilot, tables, source_workbook=[sheet]),
        lambda: tool.render_pilot_manifest(pilot),
    ):
        assert render() == render()


def _tiny_sheet(tmp_path, expected_on_title, expected_below=None):
    """造一张最小 sheet。列布局按导入器的口径:A 标签 / B 子标签 / C 取值 / D 预期结果。"""
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "13通信"
    sheet.cell(row=1, column=1, value="用例标题")
    sheet.cell(row=2, column=1, value="用例标题")
    sheet.cell(row=2, column=2, value="M_TEST_0001_01 测试用条款")
    sheet.cell(row=3, column=1, value="步骤1")
    sheet.cell(row=3, column=4, value=expected_on_title)   # 步骤标题行上的预期结果列
    sheet.cell(row=4, column=2, value="操作描述")
    sheet.cell(row=4, column=3, value="设置参数")
    sheet.cell(row=5, column=2, value="操作类型")
    sheet.cell(row=5, column=3, value="写/判断")
    if expected_below is not None:
        sheet.cell(row=6, column=4, value=expected_below)
    path = tmp_path / "tiny.xlsx"
    workbook.save(path)
    return openpyxl.load_workbook(path, data_only=True).active


def test_expected_result_written_on_the_step_title_row_is_parsed(tmp_path):
    """上游有的条款把预期结果写在**步骤标题那一行**(而不是子标签行)。

    整行 continue 会把它丢掉,下游于是报 no_expected —— 让工程师去改一个没错的地方。
    这条锁住"标题行上的内容也参与解析"。
    """
    sheet = _tiny_sheet(tmp_path, expected_on_title="读写一致为合格")
    # 表头行本身也叫「用例标题」,会产出一个空壳 case;取第一个有步骤的。
    case = next(c for c in tool.parse_sheet(sheet)["cases"] if c["steps"])

    assert case["steps"][0]["expected"] == "读写一致为合格", (
        "步骤标题行上的预期结果被丢了 —— 这正是 54 要修的缺陷"
    )


def test_a_judgment_step_without_any_expected_result_is_still_reported(tmp_path):
    """反例:标题行 E 列与子标签行都没有预期结果时,仍然必须报 no_expected。

    否则修 54 会把"真的缺预期结果"一起放过 —— 那批是要工程师补的。
    """
    sheet = _tiny_sheet(tmp_path, expected_on_title=None)
    case = next(c for c in tool.parse_sheet(sheet)["cases"] if c["steps"])

    assert case["steps"][0]["expected"] is None
    assert "写" in (case["steps"][0]["op"] or "")
