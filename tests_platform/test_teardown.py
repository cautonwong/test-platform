"""
整轮收尾(teardown)的两条路:补证据抛错时仍要断开并关句柄,清理自己失败时单独记一条、
不许顶掉正在飞的那条原异常 —— 顶掉了现场就只剩清理,真正的起因消失了。

用例只经已有的两条缝:CompositionRoot.build(清单路径) + EvidenceSession(证据目录)。
"""
import copy
import json

import pytest

from tests_support.manifest_factory import (
    METER_220V,
    MODBUS_METER_MODEL,
    build,
    meter_device,
)

from testplatform.cases import case
from testplatform.reporting.evidence import PLATFORM_ERRORS_FILE, EvidenceSession


def _manifest(devices):
    return {
        "criteria_profiles": {"meter_220v": copy.deepcopy(METER_220V)},
        "models": {"dtsd1352_modbus": copy.deepcopy(MODBUS_METER_MODEL)},
        "devices": devices,
    }


@pytest.fixture
def session(tmp_path):
    return EvidenceSession(tmp_path / "evidence")


def _platform_errors(session):
    path = session.directory / PLATFORM_ERRORS_FILE
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class _StuckDriver:
    """断开永远失败的一台设备:清理这一步自己出错。"""

    def __init__(self, driver):
        self.device = driver.device

    def disconnect(self):
        raise OSError("台架断开失败:继电器卡住")


class _WatchedDriver:
    """真断开,同时记下清理走到过它 —— 一台失败就跳过后面的,不这么看是看不出来的。"""

    def __init__(self, driver):
        self.device = driver.device
        self.disconnected = False
        self._driver = driver

    def disconnect(self):
        self._driver.disconnect()
        self.disconnected = True


def _record_round_fails(**_kwargs):
    raise OSError("证据盘满:record_round 抛错")


@case(id="T_00_00_15_000000_01", clause="free: 平台:清理", tags=["清理"])
def test_both_teardown_failures_are_recorded_and_the_original_is_raised(tmp_path, session):
    """记录抛错 → 清理照样跑完(另一台断开、句柄关掉),两条错误各记一条,抛出的仍是记录那条。"""
    root = build(
        tmp_path,
        _manifest([meter_device("meter_a", 1, 2300), meter_device("meter_b", 2, 2500)]),
        evidence=session,
    )
    watched = _WatchedDriver(root.get("meter_b"))
    root.get("meter_b").connect()
    root.get("meter_b").read_all()
    root.drivers["meter_b"] = watched
    root.drivers["meter_a"] = _StuckDriver(root.get("meter_a"))
    session.record_round = _record_round_fails

    with pytest.raises(OSError, match="证据盘满"):
        root.teardown()

    assert watched.disconnected is True
    # 句柄关掉了:Windows 上句柄没关就删不掉文件。
    session.path_of("meter_b").unlink()
    assert [(entry["nodeid"], entry["message"]) for entry in _platform_errors(session)] == [
        ("teardown", "证据盘满:record_round 抛错"),
        ("teardown:meter_a:disconnect", "台架断开失败:继电器卡住"),
    ]
