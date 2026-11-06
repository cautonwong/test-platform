"""清单的解析:YAML 字典 → Manifest。校验在 validation.py。"""
from __future__ import annotations

import difflib
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, Mapping, Tuple

import yaml

from testplatform.cases import parse_matrix_entries
from testplatform.config import paths
from testplatform.domain.criteria import Criterion, coerce_number
from testplatform.protocol.values import encode_value

from .constants import *  # noqa: F401,F403
from .constants import _allowed_access, _known_protocols
from .schema import DerivedCriterion, Manifest, Model, Point, SettingCase, SettingStep, ManualStep
from .validation import (
    _reject_unknown_keys,
    _validate_bus_address,
    _validate_can_connection,
    _validate_device_keys,
    _validate_initial_setpoint,
    _validate_point_fields,
    _validate_quantity_names,
    _validate_point_fields,
    _validate_real_mode_addresses,
    _validate_stream_connection,
    _validate_topology,
    validate_criterion_entries,
)

def parse_manifest(config: dict) -> Manifest:
    """校验整份清单并解析成型号/设备,任何一条写错都在这里抛错。"""
    _reject_unknown_keys("清单顶层", config, TOP_LEVEL_KEYS)
    profiles = _parse_criteria_profiles(config.get("criteria_profiles"))
    catalog = _parse_protocol_items(config.get("protocol_items"))
    models = _parse_models(config.get("models"), profiles, catalog)
    devices = _parse_devices(config.get("devices"), models)
    _validate_topology(devices)
    derived_criteria = _parse_derived_criteria(
        config.get("derived_criteria"), devices, models
    )
    test_matrix = _parse_test_matrix(config.get("test_matrix"), models)
    setting_cases = _parse_setting_cases(config.get("setting_cases"), models)
    return Manifest(
        models=models,
        devices=devices,
        criteria_profiles=profiles,
        test_matrix=test_matrix,
        setting_cases=setting_cases,
        derived_criteria=derived_criteria,
    )

def _parse_derived_criteria(
    raw: Any, devices: Tuple[dict, ...], models: Mapping[str, Model]
) -> Mapping[str, DerivedCriterion]:
    """派生判据:判据名 → {participants, limits}。

    三件事在装配前查掉,它们写错只会在现场表现成"判了一个没人算过的量":
      - 参与方必须是清单里真实存在的设备 id,至少两台(一台设备能读到的量用判据集就够了,
        派生的意义正是跨设备);
      - 判据名不能与参与方的读数键重名 —— 撞上之后证据里分不清判的是读数还是派生量;
      - 区间只收 min / max 且必须是数字:派生量是跨设备算出来的数,
        没有"某台设备的额定功率"可以当基准换算(那是单台设备判据的写法)。

    参与方只查到这一步为止:清单里没有公式,所以查不出"计算时有没有真用到这些设备" ——
    那由场景用例的 Python 负责,清单不为它背书。

    shortcut: 同一个判据名写两遍(YAML 同一个键重复)只有后一份生效 —— 整份清单的映射段
    都是这个行为(models 段也一样),要治先治清单加载。
    """
    derived: Dict[str, DerivedCriterion] = {}
    by_id = {spec["id"]: spec for spec in devices}
    for name, entry in (raw or {}).items():
        where = f"derived_criteria 判据 {name!r}"
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{where}: 判据名必须是非空字符串,拿到 {name!r}")
        if not isinstance(entry, dict):
            raise ValueError(
                f"{where}: 必须写成映射 {sorted(DERIVED_CRITERION_KEYS)},拿到 {entry!r}"
            )
        _reject_unknown_keys(where, entry, DERIVED_CRITERION_KEYS)
        participants = _parse_derived_participants(where, entry.get("participants"), by_id)
        _reject_derived_name_clash(where, name, participants, by_id, models)
        derived[name] = DerivedCriterion(
            name=name,
            participants=participants,
            limits=_parse_derived_limits(where, name, entry.get("limits")),
        )
    return MappingProxyType(derived)

def _parse_derived_participants(where: str, raw: Any, by_id: Mapping[str, dict]) -> Tuple[str, ...]:
    if not isinstance(raw, list) or len(raw) < 2:
        raise ValueError(
            f"{where}: participants 必须写成至少两台设备 id 的列表"
            f"(派生量是跨设备的量,一台设备的读数用判据集判),拿到 {raw!r}"
        )
    for device_id in raw:
        if not isinstance(device_id, str) or not device_id.strip():
            raise ValueError(f"{where}: participants 里的设备 id 必须是非空字符串,拿到 {device_id!r}")
        if device_id not in by_id:
            raise KeyError(
                f"{where}: participants 里的 {device_id!r} 不是清单里的设备,已声明的设备: {sorted(by_id)}"
            )
    repeated = sorted({device_id for device_id in raw if raw.count(device_id) > 1})
    if repeated:
        raise ValueError(
            f"{where}: participants 里 {repeated} 写了不止一次,同一台设备只参与一次"
        )
    return tuple(raw)

def _reject_derived_name_clash(
    where: str,
    name: str,
    participants: Tuple[str, ...],
    by_id: Mapping[str, dict],
    models: Mapping[str, Model],
) -> None:
    for device_id in participants:
        if name in models[by_id[device_id]["model"]].point_table:
            raise ValueError(
                f"{where}: 判据名与设备 {device_id!r} 的读数键重名;"
                f"派生量是算出来的量、不是读数,名字撞上读数键之后证据里分不清判的是哪一个"
                f"(换个名字,如 有功误差)"
            )

def _parse_derived_limits(where: str, name: str, raw: Any) -> Criterion:
    if not isinstance(raw, dict) or not raw:
        raise ValueError(
            f"{where}: limits 必须写成非空映射 {list(DERIVED_LIMIT_KEYS)}(如 {{min: -1.0, max: 1.0}}),"
            f"拿到 {raw!r}"
        )
    unknown = sorted(set(raw) - set(DERIVED_LIMIT_KEYS))
    if unknown:
        raise ValueError(
            f"{where}: limits 里的未知边界 {unknown},可用: {list(DERIVED_LIMIT_KEYS)}"
        )
    bounds: Dict[str, float] = {}
    for side in DERIVED_LIMIT_KEYS:
        if side not in raw:
            continue
        value = raw[side]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(
                f"{where}: limits.{side} 必须是数字(派生量是算出来的数,没有设备参数可换算),"
                f"拿到 {value!r}"
            )
        bounds[side] = float(value)
    return Criterion(quantity=name, low=bounds.get("min"), high=bounds.get("max"))

def _parse_setting_cases(raw: Any, models: Mapping[str, Model]) -> Mapping[str, SettingCase]:
    """设置类条款:型号 → 条款编号 → {clause, steps}。

    装配前把能查的都查掉,不留到运行时:
      - 引用的型号必须存在(条款写在哪个点表上);
      - 条款编号全清单唯一 —— 它是证据与归因的身份,重号就分不清是谁的记录;
      - 步骤只有 write / value / expect / manual 四个词,缺一个、多一个都是清单写错;
        manual 步骤没有可写的点,所以不能再写 write / value,expect 只能是文字说明;
      - write 的键必须是该型号点表里 writable: true 的点;
      - value 必须与那个点的 encoding / length 相容(编码那一关就是写路径那一个函数);
      - expect 必须是单个值:区间判定归判据集(equals 那一套),不在这里表达。

    shortcut: 同一条款在一个型号下写两遍(YAML 同一个键重复)只有后一份生效 ——
    整份清单的映射段都是这个行为(models 段也一样),要治先治清单加载。
    """
    cases: Dict[str, SettingCase] = {}
    for model_name, clauses in (raw or {}).items():
        where = f"setting_cases 型号 {model_name!r}"
        if model_name not in models:
            raise KeyError(f"{where}: 引用了不存在的型号,已声明的型号: {sorted(models)}")
        if not isinstance(clauses, dict) or not clauses:
            raise ValueError(
                f"{where}: 必须写成非空映射(条款编号 → {{clause, steps}}),拿到 {clauses!r}"
            )
        model = models[model_name]
        for case_id, entry in clauses.items():
            where = f"setting_cases 条款 {case_id!r}"
            if not isinstance(case_id, str) or not case_id.strip():
                raise ValueError(
                    f"{where}: 条款编号必须是非空字符串(如 M_00_00_01_000002_18),拿到 {case_id!r}"
                )
            if case_id in cases:
                raise ValueError(
                    f"{where}: 条款编号重复(型号 {cases[case_id].model!r} 下已有这一条)。"
                    f"编号是证据与归因的唯一身份,全清单只能出现一次"
                )
            cases[case_id] = _parse_setting_case(case_id, model, models, entry, where)
    return MappingProxyType(cases)

def _parse_setting_case(
    case_id: str, model: Model, models: Mapping[str, Model], entry: Any, where: str
) -> SettingCase:
    if not isinstance(entry, dict):
        raise ValueError(f"{where}: 必须写成映射 {sorted(SETTING_CASE_KEYS)},拿到 {entry!r}")
    _reject_unknown_keys(where, entry, SETTING_CASE_KEYS)
    clause = entry.get("clause")
    if not isinstance(clause, str) or not clause.strip():
        raise ValueError(
            f'{where}: clause 必须是条款清单里的 id(如 "DLT645-4.5.2"),拿到 {clause!r}'
        )
    steps = entry.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError(
            f"{where}: steps 必须是非空列表(每项一步:写 → 读回,或一步人工操作),拿到 {steps!r}"
        )
    return SettingCase(
        id=case_id,
        model=model.name,
        clause=clause,
        steps=tuple(
            _parse_setting_step(model, case_id, index, step)
            for index, step in enumerate(steps)
        ),
        bench=_parse_bench(entry.get("bench"), models, where),
    )

def _parse_bench(raw: Any, models: Mapping[str, Model], where: str) -> Mapping[str, float]:
    """条款的台架前置:量名 → 值。量名过与导入器同一份对照表,值过台架点表的编码。

    它不进 steps,也不参与判定 —— 只是"让表能测"的前提(ADR 0006),所以这里只查三件事:
    这个名字是不是那份对照表里的台架量、台架型号点表里有没有这个量、这个值台架收不收得下。
    拼错一个量名(θa 写成 ta)会静默设了个没人读的东西,所以名字不对就在这里报错。
    """
    if raw is None:
        return MappingProxyType({})
    if not isinstance(raw, dict) or not raw:
        raise ValueError(
            f"{where}: bench 必须写成非空映射(台架量名 → 值,如 {{Ua: 220, Fre: 50}}),拿到 {raw!r}"
        )
    bench_models = [model for model in models.values() if model.type == GRID_SIMULATOR_TYPE]
    if not bench_models:
        raise ValueError(
            f"{where}: 声明了 bench: 台架前置,但清单里没有 type: {GRID_SIMULATOR_TYPE} 的型号 —— "
            f"这些量没有台架可下发"
        )
    quantities = _bench_quantities()
    setpoints: Dict[str, float] = {}
    for name, value in raw.items():
        quantity = quantities.get(name) if isinstance(name, str) else None
        if quantity is None:
            close = difflib.get_close_matches(str(name), list(quantities), n=3, cutoff=0.6)
            raise KeyError(
                f"{where}: 台架量名 {name!r} 不在 {Path(BENCH_PARAMS_RELATIVE).name} 的台架量里"
                f"{f'(最接近的: {close})' if close else ''};可用的量: {sorted(quantities)}"
            )
        points = [
            model.point_table[quantity] for model in bench_models if quantity in model.point_table
        ]
        if not points:
            raise KeyError(
                f"{where}: 台架量 {name!r} 落到读数键 {quantity!r},但台架型号 "
                f"{[model.name for model in bench_models]} 的点表里没有它;"
                f"台架能设的量: {sorted({key for model in bench_models for key in model.point_table})}"
            )
        for point in points:
            if not point.writable:
                raise ValueError(
                    f"{where}: 台架点 {quantity!r} 没有声明 writable,不能下发"
                    f"(在台架型号的点表里给这一项加 writable: true)"
                )
        setpoints[quantity] = _parse_bench_value(value, f"{where} 台架量 {name!r}", points[0])
    return MappingProxyType(setpoints)

def _parse_bench_value(raw: Any, where: str, point: Point) -> float:
    """台架前置的值:必须是数值,不是名字。

    台架点现在按 ascii 占位(地址还是 TODO),写路径编不出 ascii —— 这一档就只查"是不是数值";
    真机编码到位后走与写路径同一个函数(_parse_setting_value),不另开一套口径。
    """
    value = coerce_number(raw)
    if value is None or isinstance(raw, bool):
        raise ValueError(
            f"{where}: 值必须是数值(台架要的是数而不是名字:接线方式这类枚举先在"
            f"configs/import/params.yaml 里落成整数编码),拿到 {raw!r}"
        )
    if point.encoding == "ascii":
        return value
    return _parse_setting_value(raw, where, point)

@lru_cache(maxsize=1)
def _bench_quantities() -> Mapping[str, str]:
    """台架量名 → 台架点表的读数键,读的是导入器用的同一份 configs/import/params.yaml。

    拼错在装配前报错而不是运行时:`Ua` 与 `Ub` 是两个量,写错一个字母就是另一件事。
    """
    bench_params_path = paths.resource(BENCH_PARAMS_RELATIVE)
    with bench_params_path.open("r", encoding="utf-8") as handle:
        params = yaml.safe_load(handle) or {}
    return MappingProxyType(
        {
            name: entry["quantity"]
            for name, entry in params.items()
            if isinstance(entry, dict) and entry.get("device") == "bench"
        }
    )

def _parse_setting_step(
    model: Model, case_id: str, index: int, step: Any
) -> SettingStep | ManualStep:
    where = f"setting_cases 条款 {case_id!r} 第 {index + 1} 步"
    if not isinstance(step, dict):
        raise ValueError(
            f"{where}: 必须写成映射,可用键: {list(SETTING_STEP_KEYS)},拿到 {step!r}"
        )
    _reject_unknown_keys(where, step, SETTING_STEP_KEYS)
    if "manual" in step:
        return _parse_manual_step(step, where)
    missing = [key for key in SETTING_WRITE_STEP_KEYS if key not in step]
    if missing:
        raise ValueError(
            f"{where}: 缺少 {missing},自动回环的每一步都要写全 {list(SETTING_WRITE_STEP_KEYS)}"
            f"(写哪个点、写什么值、读回来应该是什么;人工操作写成 {{manual, expect}})"
        )
    key = step["write"]
    point = model.point_table.get(key) if isinstance(key, str) else None
    if point is None:
        raise KeyError(
            f"{where}: 型号 {model.name!r} 没有点 {key!r},可用的读数键: {sorted(model.point_table)}"
        )
    if not point.writable:
        raise ValueError(
            f"{where}: 型号 {model.name!r} 的点 {key!r} 没有声明 writable,不能写;"
            f"可写的点: {[k for k, p in model.point_table.items() if p.writable]}"
            f"(在型号的点表里给这一项加 writable: true)"
        )
    expect = step["expect"]
    if not isinstance(expect, (bool, int, float, str)):
        raise ValueError(
            f"{where}: expect 必须是单个值(数字 / 字符串 / 布尔),不能是区间或列表"
            f"(区间判定归判据集),拿到 {expect!r}"
        )
    return SettingStep(
        write=key, value=_parse_setting_value(step["value"], where, point), expect=expect
    )

def _parse_manual_step(step: Mapping, where: str) -> ManualStep:
    """人工操作的一步:做什么 + 由操作员按什么确认。

    写了 write / value 就是清单写错:人工操作没有可写的点,而"写一半 + 人确认另一半"
    在报告里读不出到底哪半算数。反过来 expect 必须是文字说明 —— 一个人工步骤带着
    机读判据,下一步就会有人把它当自动步骤判过,那正是本票要防的假通过。
    """
    # expect 两种步骤都有;人工操作不共享的是写那两个(write / value)。
    extra = [key for key in ("write", "value") if key in step]
    if extra:
        raise ValueError(
            f"{where}: manual 步骤没有可写的点,不能同时写 {extra};"
            f"人工操作只写 manual(做什么)与 expect(由谁按什么确认),"
            f'如 {{manual: "切换上位机通信模式为 HDLC", expect: "由操作员确认已切换"}}'
        )
    action = step["manual"]
    if not isinstance(action, str) or not action.strip():
        raise ValueError(
            f'{where}: manual 必须是这一步人工操作做什么(非空字符串,如 "切换上位机通信模式"),'
            f"拿到 {action!r}"
        )
    if "expect" not in step:
        raise ValueError(
            f"{where}: manual 步骤缺少 expect(由谁按什么确认的文字说明)"
        )
    expect = step["expect"]
    if not isinstance(expect, str) or not expect.strip():
        raise ValueError(
            f'{where}: manual 步骤的 expect 必须是文字说明(非空字符串,如 "由操作员确认已切换"),'
            f"拿到 {expect!r} —— 人工操作没有机读判据,它不判通过也不判失败"
        )
    return ManualStep(action=action, expect=expect)

def _parse_setting_value(raw: Any, where: str, point: Point) -> float:
    """写下去的值:必须是数值,而且按这个点的 encoding / length 真能编码进去。

    "与 encoding 相容"这一关交给 encode_value —— 写路径用的就是同一个函数,
    所以装配期报错的就是运行时写不进去的那些值,不是另一套口径。BCD 点的 value 写成
    "120000" 也是这个路子:按数值收下,再按点的位数补前导零。
    """
    value = coerce_number(raw)
    if value is None:
        raise ValueError(
            f"{where}: {point.key!r} 的 value 必须是数值(按点表 encoding {point.encoding!r} "
            f"编码写下去;非数字字符没法编成寄存器字节),拿到 {raw!r}"
        )
    try:
        encode_value(
            value,
            point.encoding,
            point.length,
            point.scale,
            point.byteorder,
            point.word_order,
        )
    except ValueError as exc:
        raise ValueError(
            f"{where}: {point.key!r} 的 value {raw!r} 与点表项不相容,写不进去: {exc}"
        ) from None
    return value

def _parse_test_matrix(raw: Any, models: Mapping[str, Model]) -> Mapping[str, Tuple[Selector, ...]]:
    """适用矩阵:型号 → 用例编号 / 标签。型号写错在装配前就报错;
    编号和标签对不对要等收集到用例才知道(另一半校验在 testplatform/plugin.py)。"""
    matrix: Dict[str, Tuple[Selector, ...]] = {}
    for model_name, entries in (raw or {}).items():
        where = f"test_matrix 型号 {model_name!r}"
        if model_name not in models:
            raise KeyError(f"{where}: 引用了不存在的型号,已声明的型号: {sorted(models)}")
        if not isinstance(entries, list):
            raise ValueError(f"{where}: 必须写成列表(用例编号或 tag:<标签>),拿到 {entries!r}")
        matrix[model_name] = parse_matrix_entries(model_name, entries)
    return MappingProxyType(matrix)

def _parse_criteria_profiles(raw: Any) -> Mapping[str, list]:
    profiles: Dict[str, list] = {}
    for name, entries in (raw or {}).items():
        profiles[name] = validate_criterion_entries(entries, f"判据集 {name!r}")
    return MappingProxyType(profiles)

def _parse_protocol_items(raw: Any) -> Mapping[str, Mapping[str, dict]]:
    """协议级数据项目录:协议 → DI → 这一项的固有事实。

    目录不属于任何型号:同一个 DI 在所有型号上的长度 / 编码 / 字节序都一样,
    重复写在每个型号里就是几万份可以各自抄错的东西(scale: 0.01 抄成 0.1 是一个
    看起来正常的假读数)。这里只放"规约决定的东西",型号差异留在点表里覆盖。
    协议名和目录项字段都走闭合集合校验:拼错一个 DI 或字段名在这里就报错。
    """
    catalog: Dict[str, Mapping[str, dict]] = {}
    for protocol, items in (raw or {}).items():
        if _allowed_access(protocol) is None:
            raise ValueError(
                f"protocol_items 协议 {protocol!r}: 未知规约,已登记的规约: {_known_protocols()}"
            )
        if not isinstance(items, dict) or not items:
            raise ValueError(
                f"protocol_items 协议 {protocol!r}: 必须写成非空映射(DI → 目录项),拿到 {items!r}"
            )
        entries: Dict[str, dict] = {}
        for di, entry in items.items():
            where = f"protocol_items 协议 {protocol!r} 项 {di!r}"
            if not isinstance(di, str) or not di.strip():
                raise ValueError(f"{where}: DI 必须是非空字符串(如 \"00010000\"),拿到 {di!r}")
            if not isinstance(entry, dict):
                raise ValueError(f"{where}: 必须写成映射,拿到 {entry!r}")
            _reject_unknown_keys(where, entry, PROTOCOL_ITEM_KEYS)
            missing = [field for field in POINT_FIELD_REQUIRED if field not in entry]
            if missing:
                raise ValueError(
                    f"{where}: 缺少字段 {missing},必填字段: {list(POINT_FIELD_REQUIRED)}"
                    f"(长度和编码由 DI 决定,正是从型号里搬进目录的那部分)"
                )
            name = entry.get("name")
            if name is not None and (not isinstance(name, str) or not name.strip()):
                raise ValueError(f"{where}: name 必须是 DI 的语义名(非空字符串),拿到 {name!r}")
            _validate_point_fields(where, entry, protocol)
            entries[di] = entry
        catalog[protocol] = MappingProxyType(entries)
    return MappingProxyType(catalog)

def _parse_models(
    raw: Any, profiles: Mapping[str, list], catalog: Mapping[str, Mapping[str, dict]]
) -> Mapping[str, Model]:
    models: Dict[str, Model] = {}
    for name, entry in (raw or {}).items():
        where = f"型号 {name!r}"
        if not isinstance(entry, dict):
            raise ValueError(f"{where}: 必须写成映射,拿到 {entry!r}")
        missing = [key for key in MODEL_REQUIRED if key not in entry]
        if missing:
            raise ValueError(f"{where}: 缺少字段 {missing},必填字段: {list(MODEL_REQUIRED)}")
        _reject_unknown_keys(where, entry, MODEL_KEYS)

        protocol = entry["protocol"]
        if _allowed_access(protocol) is None:
            raise ValueError(
                f"{where}: 未知规约 {protocol!r},已登记的规约: {_known_protocols()}"
            )
        criteria = entry.get("criteria")
        if criteria is not None and criteria not in profiles:
            raise KeyError(
                f"{where}: 引用了不存在的判据集 {criteria!r},已声明: {sorted(profiles)}"
            )
        models[name] = Model(
            name=name,
            type=entry["type"],
            protocol=protocol,
            criteria=criteria,
            capabilities=_parse_capabilities(name, entry["capabilities"]),
            point_table=_parse_point_table(
                name, entry["point_table"], protocol, catalog.get(protocol, {})
            ),
            device_params=_parse_device_params(name, entry.get("device_params")),
        )
        _validate_quantity_names(name, entry["type"], models[name].point_table)
    return MappingProxyType(models)

def _parse_capabilities(model_name: str, raw: Any) -> frozenset:
    if not isinstance(raw, list) or not all(isinstance(c, str) and c for c in raw):
        raise ValueError(f"型号 {model_name!r}: capabilities 必须写成能力名列表,拿到 {raw!r}")
    return frozenset(raw)

def _parse_device_params(model_name: str, raw: Any) -> frozenset:
    """型号声明它接受哪些设备级参数。声明了就必填:设备少给一个也是清单写错了。"""
    if raw is None:
        return frozenset()
    if not isinstance(raw, list) or not all(isinstance(p, str) and p.strip() for p in raw):
        raise ValueError(f"型号 {model_name!r}: device_params 必须写成参数名列表,拿到 {raw!r}")
    clash = sorted(set(raw) & set(CORE_DEVICE_KEYS))
    if clash:
        raise ValueError(
            f"型号 {model_name!r}: device_params 里的 {clash} 是设备实例的核心键,不能当设备参数声明"
        )
    return frozenset(raw)

def _parse_point_table(
    model_name: str, raw: Any, protocol: str, catalog: Mapping[str, dict]
) -> Mapping[str, Point]:
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"型号 {model_name!r}: point_table 必须是非空映射(读数键 → 点表项)")
    points: Dict[str, Point] = {}
    for key, entry in raw.items():
        points[key] = _parse_point(model_name, key, entry, protocol, catalog)
    return MappingProxyType(points)

def _parse_point(
    model_name: str, key: Any, entry: Any, protocol: str, catalog: Mapping[str, dict]
) -> Point:
    """点表项二选一:内联(写全 address/length/encoding),或 {item: <DI>} 引用协议目录。"""
    if not isinstance(key, str) or not key.strip():
        raise ValueError(f"型号 {model_name!r}: 点表项的键是读数键,必须是非空字符串")
    where = f"型号 {model_name!r} 点表项 {key!r}"
    if not isinstance(entry, dict):
        raise ValueError(f"{where}: 必须写成映射,拿到 {entry!r}")
    if "item" in entry:
        address, entry = _resolve_item(where, entry, protocol, catalog)
    else:
        missing = [field for field in POINT_REQUIRED if field not in entry]
        if missing:
            raise ValueError(
                f"{where}: 缺少字段 {missing},必填字段: {list(POINT_REQUIRED)}"
                f"(或写成 {{item: <DI>}} 引用 protocol_items 里的协议目录)"
            )
        _reject_unknown_keys(where, entry, POINT_KEYS)
        address = entry["address"]
        if not (
            isinstance(address, int) and not isinstance(address, bool) and address >= 0
        ) and not (isinstance(address, str) and address.strip()):
            raise ValueError(
                f"{where}: address 必须是非负整数或非空字符串(OBIS 逻辑名),拿到 {address!r}"
            )
    return Point(key=key, address=address, **_validate_point_fields(where, entry, protocol))

def _resolve_item(
    where: str, entry: Mapping, protocol: str, catalog: Mapping[str, dict]
) -> Tuple[str, Mapping]:
    """把 {item: <DI>, ...覆盖} 合上目录项,返回 (地址, 合并后的字段)。

    覆盖是通用的:写了哪个字段就覆盖哪个(encoding 和 scale 走同一条路,没有特判)。
    不许两处真相:address 与 item 说的是同一件事;与目录项逐字相同的字段是重复声明,
    两种都在装配前报错 —— 否则同一个事实有两个地方可以改,就一定有地方被改漏。
    """
    if "address" in entry:
        raise ValueError(
            f"{where}: item {entry['item']!r} 已经指明了这个数据项的地址,"
            f"又内联写了 address: {entry['address']!r}"
            f" —— 同一个字段两处真相。引用写法只写 item 和要覆盖的字段,地址由目录决定"
        )
    _reject_unknown_keys(where, entry, POINT_REFERENCE_KEYS)
    item = entry["item"]
    if not isinstance(item, str) or not item.strip():
        raise ValueError(
            f"{where}: item 必须是协议目录里的 DI(非空字符串,如 \"00010000\"),拿到 {item!r}"
        )
    catalog_item = catalog.get(item)
    if catalog_item is None:
        raise KeyError(
            f"{where}: 引用了协议 {protocol!r} 目录里不存在的 DI {item!r};该目录已声明"
            f" {len(catalog)} 个 DI{_closest_di_hint(item, catalog)}。DI 只在 protocol_items 里登记一次,"
            f"新用到的 DI 先写进协议目录"
        )
    overrides = {field: value for field, value in entry.items() if field != "item"}
    repeated = sorted(
        field for field, value in overrides.items() if catalog_item.get(field) == value
    )
    if repeated:
        raise ValueError(
            f"{where}: {repeated} 与 protocol_items 里 {protocol!r} 的 {item!r} 逐字相同,"
            f"是同一个字段的两处写法;删掉这些字段(只写与目录不同的那些,写了的字段就是覆盖)"
        )
    return item, {**catalog_item, **overrides}

def _closest_di_hint(item: str, catalog: Mapping[str, dict]) -> str:
    """拼错的 DI 报"最接近的几个"。目录有两万个 DI 时不把它们全打出来。"""
    close = difflib.get_close_matches(item, list(catalog), n=3, cutoff=0.8)
    return f"(最接近的: {close})" if close else ""

def _parse_devices(raw: Any, models: Mapping[str, Model]) -> Tuple[dict, ...]:
    devices = []
    seen = set()
    for index, spec in enumerate(raw or []):
        where = f"devices 第 {index + 1} 条"
        if not isinstance(spec, dict):
            raise ValueError(f"{where}: 必须写成映射,拿到 {spec!r}")
        device_id = spec.get("id")
        if not device_id:
            raise ValueError(f"{where}: 缺少 id")
        where = f"设备 {device_id!r}"
        for key in MODEL_ONLY_DEVICE_KEYS:
            if key in spec:
                raise ValueError(
                    f"{where}: {key!r} 是型号级参数,必须写在 models 的那个型号上,"
                    f"设备实例只允许写核心键 {sorted(CORE_DEVICE_KEYS)} 和型号声明的设备参数"
                )
        if device_id in seen:
            raise ValueError(f"{where}: id 重复,设备 id 必须唯一")
        seen.add(device_id)

        model_name = spec.get("model")
        if model_name is None:
            raise ValueError(f"{where}: 缺少 model,设备实例只引用型号,不内联点表")
        if model_name not in models:
            raise KeyError(
                f"{where}: 引用了不存在的型号 {model_name!r},已声明的型号: {sorted(models)}"
            )
        model = models[model_name]
        _validate_device_keys(where, spec, model)
        role = spec.get("role", DEFAULT_ROLE)
        if role not in DEVICE_ROLES:
            raise ValueError(
                f"{where}: 未知角色 {role!r},可用角色: {list(DEVICE_ROLES)}"
                f"(不写 role 时缺省 {DEFAULT_ROLE!r})"
            )
        _validate_initial_setpoint(where, spec.get("initial_setpoint"), role)
        _validate_real_mode_addresses(where, model, spec.get("mode"))

        access = spec.get("access")
        if access is None:
            raise ValueError(f"{where}: 缺少 access(接入方式),如 tcp / serial / gateway_ssh")
        # connection 里的硬件事实先校验:CAN 的接口类型决定用哪个驱动(这台机器上装了什么),
        # 写错要在装配前报错并列出可用值 —— 不能等 python-can 抛 "Interface 'socket' not found"。
        # 放在"型号能不能走这个接入方式"之前:字段写错了就先报字段,不拿一句"不能配 can"糊过去。
        if access == "can":
            _validate_can_connection(where, spec.get("connection"))
        elif spec.get("via") is None:
            # 下游设备借上游总线、不建物理连接:它的 connection 归拓扑校验管
            # (_validate_topology 直接拒绝),这里不重复判。
            _validate_stream_connection(where, access, spec.get("connection"), spec.get("mode"))
        allowed = _allowed_access(model.protocol)
        if allowed is None:  # 型号解析时已经挡住,这里只是防御
            raise ValueError(
                f"{where}: 未知规约 {model.protocol!r},已登记的规约: {_known_protocols()}"
            )
        if access not in allowed and access in TRANSPORT_ONLY_ACCESS:
            raise ValueError(
                f"{where}: 接入方式 {access!r} 的传输已经实现并注册(transport/real_transports.py,"
                f"registry.py 里注册为 {access!r}),但还没有任何规约声明支持它;"
                f"型号 {model_name!r} 的规约是 {model.protocol!r},它跑在 {sorted(allowed)} 上,"
                f"不跑 {access!r}。要接这类设备:先定该设备用什么规约上 {access.upper()}"
                f"(如 EMS 走 REST、HEMS 走 MQTT 上报、工控机走 SSH 命令),"
                f"然后在对应驱动里 register_protocol(..., access={{...}})"
            )
        if access not in allowed:
            raise ValueError(
                f"{where}: 型号 {model_name!r} 的规约是 {model.protocol!r},"
                f"不能配接入方式 {access!r};该规约允许的接入方式: {sorted(allowed)}"
            )
        if model.protocol in BUS_ADDRESS_PROTOCOLS:
            _validate_bus_address(where, spec.get("bus_address"), model_name, model.protocol)
        validate_criterion_entries(spec.get("criteria_override"), f"{where} criteria_override")
        # 角色在解析时补齐,下游(组合根 / 测试层)只读 spec["role"],不再各自猜缺省值。
        devices.append({**spec, "role": role})
    return tuple(devices)
