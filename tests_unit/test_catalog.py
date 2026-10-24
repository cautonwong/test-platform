"""
资产目录(票 08):扫描生成 + 八项引用完整性 + 可校验 + 规模。

每条引用断裂一条用例:目录必须报出**是哪一条**(gap 的 where 精确到型号/设备/编号),
而且一次运行把所有缺口都报出来(不是遇到第一处就停)。另有两条运维底线:
重新生成逐字节一致、`--verify` 能把"多出 / 缺失 / 变了"分开说。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
import yaml

from testplatform.catalog import catalog as assets

REPO = Path(__file__).resolve().parents[1]

CLAUSES = """
clauses:
  C-1: {standard: S, number: "1", summary: 用到的条款}
  C-2: {standard: S, number: "2", summary: 没人用的条款}
"""

# 只有被用例引用到的那一条:用来断言"干净的一棵树没有缺口"。
CLAUSES_ONLY_C1 = """
clauses:
  C-1: {standard: S, number: "1", summary: 用到的条款}
"""

METER_MODEL = {
    "type": "meter",
    "protocol": "modbus_rtu",
    "criteria": "P-1",
    "capabilities": ["电压"],
    "point_table": {"电压": {"address": 2, "length": 2, "encoding": "uint", "scale": 0.1}},
}
CLEAN_MANIFEST = {
    "criteria_profiles": {"P-1": [{"quantity": "电压", "min": 198, "max": 242}]},
    "models": {"meter_m": METER_MODEL},
    "devices": [
        {
            "id": "meter_01",
            "model": "meter_m",
            "access": "serial",
            "mode": "simulator",
            "connection": {"simulated_registers": {2: 2300}},
        }
    ],
}

CLEAN_CASE = '''
from testplatform.cases import case

CLAUSE = "C-1"


@case(id="M_00_00_01_000000_01", clause=CLAUSE, tags=["一致性"])
def test_meter_reads(meter):
    """按设备类型参数化:归属到 meter 类型的所有型号。"""
'''


def _tree(tmp_path, *, manifest=None, clause_text=CLAUSES, files=None):
    """一棵最小的"仓库":清单 + 条款清单 + 一个测试目录。"""
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "devices.yaml").write_text(
        yaml.safe_dump(manifest if manifest is not None else CLEAN_MANIFEST, allow_unicode=True),
        encoding="utf-8",
    )
    (tmp_path / "configs" / "clauses.yaml").write_text(clause_text, encoding="utf-8")
    tests = tmp_path / "tests_probe"
    tests.mkdir()
    for name, text in (files or {"test_probe.py": CLEAN_CASE}).items():
        (tests / name).write_text(text, encoding="utf-8")
    return tmp_path


def _build(tree):
    return assets.build(
        tree / "configs" / "devices.yaml",
        clauses_path=tree / "configs" / "clauses.yaml",
        test_roots=[tree / "tests_probe"],
    )


def _gaps(catalog, kind=None):
    return [gap for gap in catalog.gaps if kind is None or gap["kind"] == kind]


def _case_file(*, case_id="M_00_00_01_000000_01", clause_id="C-1", fixture="meter", name="test_probe"):
    return f'''
from testplatform.cases import case

CLAUSE = "{clause_id}"


@case(id="{case_id}", clause=CLAUSE, tags=["一致性"])
def {name}({fixture}):
    """探针用例。"""
'''


# ------------------------------------------------------------- 生成 ----

def test_a_clean_tree_has_no_gaps(tmp_path):
    catalog = _build(_tree(tmp_path, clause_text=CLAUSES_ONLY_C1))

    assert catalog.gaps == [], catalog.gaps
    assert set(catalog.models) == {"meter_m"}
    assert set(catalog.cases) == {"M_00_00_01_000000_01"}
    assert catalog.models["meter_m"]["cases"] == ["M_00_00_01_000000_01"]
    assert catalog.models["meter_m"]["clauses"] == ["C-1"]


def test_regeneration_is_byte_identical(tmp_path):
    """派生数据:同一份输入扫两遍,产物逐字节一致(否则 --verify 每天都会喊"变了")。"""
    tree = _tree(tmp_path)

    first = assets.dumps(_build(tree))
    second = assets.dumps(_build(tree))

    assert first == second


def test_the_human_report_carries_counts_gaps_and_models(tmp_path):
    tree = _tree(tmp_path, files={"test_probe.py": _case_file(clause_id="C-1")})

    markdown = assets.render(_build(tree))

    assert "资产目录(生成物,不要手改)" in markdown
    assert "型号 1 · 设备 1" in markdown
    assert "条款零覆盖(1)" in markdown
    assert "| meter_m |" in markdown


def test_a_case_is_attributed_to_models_of_the_fixture_type_and_narrowed_by_the_matrix(tmp_path):
    """归属靠"函数引用了哪个设备类型的泛型 fixture";矩阵有定义就按矩阵收窄。"""
    manifest = {
        **CLEAN_MANIFEST,
        "models": {
            "meter_m": METER_MODEL,
            "meter_n": {**METER_MODEL, "criteria": "P-1"},
        },
        "devices": CLEAN_MANIFEST["devices"]
        + [{"id": "meter_02", "model": "meter_n", "access": "serial", "mode": "simulator"}],
        "test_matrix": {"meter_n": ["tag:一次性"]},
    }
    tree = _tree(tmp_path, manifest=manifest, files={"test_probe.py": _case_file()})

    catalog = _build(tree)

    assert catalog.models["meter_m"]["cases"] == ["M_00_00_01_000000_01"]
    assert catalog.models["meter_n"]["cases"] == [], "矩阵里没有这条用例,型号就不该算它有覆盖"


# ------------------------------------------------- 八项引用完整性 ----

def test_a_device_pointing_at_a_missing_model_is_reported(tmp_path):
    manifest = {
        **CLEAN_MANIFEST,
        "devices": [{"id": "ghost_01", "model": "no_such_model", "access": "serial"}],
    }
    catalog = _build(_tree(tmp_path, manifest=manifest))

    (gap,) = _gaps(catalog, assets.GAP_MISSING_MODEL)
    assert gap["where"] == "devices.ghost_01"
    assert "no_such_model" in gap["detail"] and "meter_m" in gap["detail"]


def test_a_model_pointing_at_a_missing_criteria_profile_is_reported(tmp_path):
    manifest = {
        **CLEAN_MANIFEST,
        "models": {"meter_m": {**METER_MODEL, "criteria": "P-missing"}},
    }
    catalog = _build(_tree(tmp_path, manifest=manifest))

    (gap,) = _gaps(catalog, assets.GAP_MISSING_CRITERIA_PROFILE)
    assert gap["where"] == "models.meter_m"
    assert "P-missing" in gap["detail"] and "P-1" in gap["detail"]


def test_a_criteria_quantity_outside_the_point_table_is_reported(tmp_path):
    manifest = {
        **CLEAN_MANIFEST,
        "criteria_profiles": {"P-1": [{"quantity": "有功电量", "min": 0}]},
    }
    catalog = _build(_tree(tmp_path, manifest=manifest))

    (gap,) = _gaps(catalog, assets.GAP_QUANTITY_NOT_IN_POINT_TABLE)
    assert gap["where"] == "models.meter_m"
    assert "有功电量" in gap["detail"] and "电压" in gap["detail"]


def test_a_driver_mapped_domain_quantity_is_not_a_gap(tmp_path):
    """inverter 的判据量是驱动映射出来的域量(output_power_w),不是点表键 —— 不许误报。"""
    manifest = {
        "criteria_profiles": {"P-1": [{"quantity": "output_power_w", "min": 0}]},
        "models": {
            "inverter_m": {
                "type": "inverter",
                "protocol": "vendor_ascii",
                "criteria": "P-1",
                "capabilities": ["有功功率"],
                "device_params": ["rated_power_w"],
                "point_table": {
                    "有功功率": {"address": 0, "length": 8, "encoding": "ascii", "unit": "W"}
                },
            }
        },
        "devices": [
            {"id": "inv_01", "model": "inverter_m", "access": "tcp", "rated_power_w": 5000}
        ],
    }
    catalog = _build(_tree(tmp_path, manifest=manifest))

    assert _gaps(catalog, assets.GAP_QUANTITY_NOT_IN_POINT_TABLE) == []


def test_a_capability_outside_the_point_table_is_reported(tmp_path):
    manifest = {
        **CLEAN_MANIFEST,
        "models": {"meter_m": {**METER_MODEL, "capabilities": ["电压", "有功电量"]}},
    }
    catalog = _build(_tree(tmp_path, manifest=manifest))

    (gap,) = _gaps(catalog, assets.GAP_CAPABILITY_NOT_IN_POINT_TABLE)
    assert gap["where"] == "models.meter_m"
    assert "有功电量" in gap["detail"]


def test_two_files_declaring_the_same_case_id_are_reported_with_both_places(tmp_path):
    """编号全局唯一**不依赖收集期**:目录扫源文件,所以 `--select` 跑子集时这条照样报。"""
    tree = _tree(
        tmp_path,
        files={"test_a.py": _case_file(name="test_a"), "test_b.py": _case_file(name="test_b")},
    )

    catalog = _build(tree)

    (gap,) = _gaps(catalog, assets.GAP_DUPLICATE_CASE_ID)
    assert gap["where"] == "M_00_00_01_000000_01"
    assert "test_a.py" in gap["detail"] and "test_b.py" in gap["detail"]


def test_a_matrix_or_setting_case_naming_a_missing_model_is_reported(tmp_path):
    manifest = {
        **CLEAN_MANIFEST,
        "test_matrix": {"ghost_model": ["tag:一致性"]},
        "setting_cases": {"ghost_model": {}},
    }
    catalog = _build(_tree(tmp_path, manifest=manifest))

    where = {gap["where"] for gap in _gaps(catalog, assets.GAP_CASE_WITHOUT_MODEL)}
    assert where == {"test_matrix.ghost_model", "setting_cases.ghost_model"}


def test_a_model_whose_protocol_has_no_simulator_is_reported(tmp_path):
    manifest = {
        **CLEAN_MANIFEST,
        "models": {"meter_m": {**METER_MODEL, "protocol": "no_such_protocol"}},
    }
    catalog = _build(_tree(tmp_path, manifest=manifest))

    (gap,) = _gaps(catalog, assets.GAP_MODEL_WITHOUT_SIMULATOR)
    assert gap["where"] == "models.meter_m"
    assert "CI" in gap["detail"] or "模拟器" in gap["detail"]


def test_a_clause_nobody_references_is_reported(tmp_path):
    catalog = _build(_tree(tmp_path))

    (gap,) = _gaps(catalog, assets.GAP_CLAUSE_WITHOUT_CASES)
    assert gap["where"] == "C-2"


def test_a_case_id_that_is_not_a_literal_is_reported_instead_of_guessed(tmp_path):
    """编号由函数算出来时,目录不猜:报一条"不是字面量",让人改成字面量或登记进清单。"""
    tree = _tree(
        tmp_path,
        files={
            "test_probe.py": '''
from testplatform.cases import case

CLAUSE = "C-1"


def make_id():
    return "M_dynamic"


@case(id=make_id(), clause=CLAUSE, tags=["一致性"])
def test_probe(meter):
    pass
'''
        },
    )

    catalog = _build(tree)

    (gap,) = _gaps(catalog, assets.GAP_UNRESOLVED_CASE_ID)
    assert "test_probe.py" in gap["where"]


# --------------------------------------------------------- 跨平台指纹 ----

def test_case_paths_are_posix_so_the_fingerprint_is_cross_platform(tmp_path):
    """目录是入库产物:Windows 上生成的路径若带反斜杠,Linux CI 重新扫出来就对不上(假红)。

    所以在 Linux 和 Windows 上跑出来的 `catalog.json` 必须逐字节一致 —— 这条钉住分隔符。
    """
    catalog = _build(_tree(tmp_path))

    for case_id, record in catalog.cases.items():
        assert "\\" not in record["file"], f"{case_id} 的路径带了反斜杠: {record['file']!r}"
        assert record["file"].endswith(".py")


def test_the_committed_catalog_is_cross_platform(tmp_path):
    """入库的那份也一样:里面不许出现反斜杠路径。"""
    written = json.loads((REPO / "catalog" / "catalog.json").read_text(encoding="utf-8"))

    bad = {
        case_id: record["file"]
        for case_id, record in written["cases"].items()
        if "\\" in record["file"]
    }
    assert bad == {}, f"入库目录里有 Windows 路径(CI 会漂): {bad}"


def test_the_committed_catalog_matches_a_fresh_scan_of_the_repository():
    """入库的 `catalog/` 必须等于「现在扫一遍仓库」的结果 —— 用例搬走/新增而没重新生成,这条就红。

    上面的 `test_regeneration_is_byte_identical` 只验「同一份输入扫两遍一致」(确定性),
    从不拿仓库里那份比:17b 的 CI 断言只验 job 里写着 `--verify`,于是目录漂了三个月照样绿。
    这条补的是结果那一侧。
    """
    built = assets.build(
        REPO / "configs" / "devices.yaml",
        clauses_path=REPO / "configs" / "clauses.yaml",
        test_roots=[REPO / name for name in assets.DEFAULT_TEST_ROOTS],
    )

    differences = assets.verify(built, REPO / "catalog")

    assert differences == [], (
        "入库的 catalog/ 与「现在扫一遍仓库」的结果不一致(它是生成物):\n  "
        + "\n  ".join(differences)
        + "\n重新生成:python tools/catalog.py"
    )


# --------------------------------------------------------------- 校验 ----

def test_verify_is_silent_when_the_catalog_matches_disk(tmp_path):
    tree = _tree(tmp_path)
    out = tmp_path / "catalog"
    assets.write(_build(tree), out)

    assert assets.verify(_build(tree), out) == []


def test_verify_reports_added_removed_and_changed_entries(tmp_path):
    tree = _tree(tmp_path)
    out = tmp_path / "catalog"
    assets.write(_build(tree), out)

    written = json.loads((out / assets.CATALOG_FILE).read_text(encoding="utf-8"))
    written["cases"]["M_00_00_01_000000_01"]["line"] = 999
    # 一条"磁盘上有、扫描不到"的用例:字段照抄已有那条(不新写 clause 字面量,07 的守卫会扫到)
    written["cases"]["M_ghost"] = dict(written["cases"]["M_00_00_01_000000_01"])
    del written["models"]["meter_m"]
    (out / assets.CATALOG_FILE).write_text(
        json.dumps(written, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    differences = assets.verify(_build(tree), out)

    joined = "\n".join(differences)
    assert "cases: 变了 'M_00_00_01_000000_01'" in joined
    assert "cases: 缺失 'M_ghost'" in joined
    assert "models: 多出 'meter_m'" in joined


def test_verify_asks_to_generate_first_when_the_catalog_is_missing(tmp_path):
    (difference,) = assets.verify(_build(_tree(tmp_path)), tmp_path / "catalog")
    assert "先跑一次" in difference


# --------------------------------------------------------------- 规模 ----

def test_scanning_scales_to_ten_thousand_cases(tmp_path):
    """规模:1 万条用例 / 200 个文件。逐文件 AST 扫描(不 import、不整棵读),给出实测耗时。"""
    files = {}
    for file_index in range(200):
        body = ["from testplatform.cases import case", 'CLAUSE = "C-1"', ""]
        for case_index in range(50):
            case_id = f"M_99_{file_index:04d}_{case_index:04d}"
            body += [
                f'@case(id="{case_id}", clause=CLAUSE, tags=["一致性"])',
                f"def test_case_{file_index}_{case_index}(meter):",
                "    pass",
                "",
            ]
        files[f"test_bulk_{file_index:03d}.py"] = "\n".join(body)
    tree = _tree(tmp_path, files=files)

    started = time.monotonic()
    catalog = _build(tree)
    elapsed = time.monotonic() - started

    assert len(catalog.cases) == 10_000
    assert elapsed < 30, f"1 万条用例扫了 {elapsed:.1f}s,超出预算"
    print(f"\n规模:200 个文件 / 10000 条用例,扫描+生成 {elapsed:.2f}s")


# ------------------------------------------- 已知豁免:验不了的条款(票 18) ----

WAIVED_CLAUSES = """
clauses:
  C-1: {standard: S, number: "1", summary: 用到的条款}
  C-2:
    standard: S
    number: "2"
    summary: 验不了的那条
    not_applicable: "本仓库没有这条条款的原文,也没有对应到它的量:TODO(来源)"
"""


def test_a_waived_clause_is_not_a_gap_but_stays_visible(tmp_path):
    """清单里写了"验不了+理由"的条款:不算缺口,但必须出现在 waived 里(不许沉默)。"""
    tree = _tree(tmp_path, clause_text=WAIVED_CLAUSES)

    catalog = _build(tree)

    assert _gaps(catalog, assets.GAP_CLAUSE_WITHOUT_CASES) == []
    assert [entry["where"] for entry in catalog.waived] == ["C-2"]
    assert "TODO(来源)" in catalog.waived[0]["reason"]
    assert catalog.as_record()["clauses"]["C-2"]["not_applicable"], "条款记录里也要带理由"
    assert "已知豁免" in assets.render(catalog)


def test_changing_a_case_clause_leaves_the_old_clause_uncovered(tmp_path):
    """守卫:把用例的 clause 改掉 → 原条款就没人验了,目录必须报出来。"""
    tree = _tree(tmp_path, clause_text=CLAUSES_ONLY_C1)
    assert _build(tree).gaps == []

    # 把那条用例改挂到另一条条款上(C-2 未被豁免 → 它零覆盖就是缺口;C-1 变成零覆盖)
    (tree / "tests_probe" / "test_probe.py").write_text(
        _case_file(clause_id="C-2"), encoding="utf-8"
    )

    catalog = _build(tree)

    uncovered = {gap["where"] for gap in _gaps(catalog, assets.GAP_CLAUSE_WITHOUT_CASES)}
    assert uncovered == {"C-1"}, uncovered


# ------------------------------------- verify 不许漏比(票 12a) ----

def _written_catalog(tree, out):
    assets.write(_build(tree), out)
    return json.loads((out / assets.CATALOG_FILE).read_text(encoding="utf-8"))


def _rewrite(out, record):
    (out / assets.CATALOG_FILE).write_text(
        json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


def test_verify_catches_a_changed_version(tmp_path):
    """格式版本被改掉 → 红(后面的比较本来就依赖它)。"""
    tree = _tree(tmp_path)
    out = tmp_path / "catalog"
    record = _written_catalog(tree, out)
    record["version"] = record["version"] + 1
    _rewrite(out, record)

    assert any("version:" in line for line in assets.verify(_build(tree), out))


def test_verify_catches_a_changed_count(tmp_path):
    """各段计数是"这一批有多少东西"的摘要,被改掉必须看得见。"""
    tree = _tree(tmp_path)
    out = tmp_path / "catalog"
    record = _written_catalog(tree, out)
    record["counts"]["cases"] = record["counts"]["cases"] + 5
    _rewrite(out, record)

    assert any("counts.cases:" in line for line in assets.verify(_build(tree), out))


def test_verify_catches_a_tampered_waiver(tmp_path):
    """已知豁免是"这条标准我们不验"的唯一记录:理由被改、条目被删,都必须红。"""
    tree = _tree(tmp_path, clause_text=WAIVED_CLAUSES)
    out = tmp_path / "catalog"
    record = _written_catalog(tree, out)
    assert record["waived"], "前提:这份清单里有一条已知豁免"

    # ① 改理由
    record["waived"][0]["reason"] = "随便改一句"
    _rewrite(out, record)
    assert any("waived: 变了" in line for line in assets.verify(_build(tree), out))

    # ② 磁盘上删掉条目(豁免记录消失)→ 现在扫出来的这条就是"多出"(与 gaps 同一套措辞)
    record = _written_catalog(tree, out)
    record["waived"] = []
    _rewrite(out, record)
    assert any("waived: 多出" in line for line in assets.verify(_build(tree), out))

    # ③ 豁免不许被当成缺口来比:它的差异行必须叫 waived,不是 gaps
    lines = assets.verify(_build(tree), out)
    assert all("waived:" in line for line in lines if "C-2" in line), lines
