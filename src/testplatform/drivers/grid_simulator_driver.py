"""
GridSimulatorDriver —— 周边设备的驱动,结构和 InverterDriver / BatteryDriver 完全对称。

周边设备不是另一套机制:同一个 @register_device 注册表、同一套 transport、
同一份型号与点表,清单里只是多了 `role: support`。所以再新增一种周边设备类型
(可调负载、标准源……)也只是新建 domain + driver 文件 + 清单条目,
组合根和测试层都不用改分支。
"""
from __future__ import annotations

from typing import Mapping

from testplatform.config.manifest import Model
from testplatform.domain.criteria import CriteriaProfile
from testplatform.domain.grid_simulator import GridSetpoint, GridSimulator, ThreePhaseSetpoint
from testplatform.transport.interfaces import ByteStreamTransport

from .registry import register_device


class GridSimulatorDriver:
    def __init__(self, grid_simulator: GridSimulator, transport: ByteStreamTransport) -> None:
        self._grid_simulator = grid_simulator
        self._transport = transport

    def connect(self) -> None:
        self._transport.connect()
        self._grid_simulator.mark_connected()

    def disconnect(self) -> None:
        self._transport.disconnect()
        self._grid_simulator.mark_disconnected()

    def apply_grid(self, voltage_v: float, freq_hz: float) -> GridSetpoint:
        """设定电网;应答里的实际输出值才是记进聚合根的那个值。"""
        self._transport.send(f"SET_GRID {voltage_v} {freq_hz}\n".encode())
        raw = self._transport.receive(timeout=2.0)
        applied_v, applied_hz = (float(x) for x in raw.decode().strip().split())
        return self._grid_simulator.apply_setpoint(applied_v, applied_hz)

    def apply_three_phase(self, values: Mapping[str, float]) -> ThreePhaseSetpoint:
        """下发一块三相台架设定(Ua=Un / Ia=0 / Fre=Hz …);应答里的实际值才算数。

        帧里是 `量名=值`,量名就是点表的读数键(上游写法),驱动不翻译、不合成「电压」。
        真机的帧格式与地址还没定义(见型号点表里的占位地址),这里先定形状。
        """
        frame = " ".join(f"{name}={value}" for name, value in values.items())
        self._transport.send(f"SET_GRID3 {frame}\n".encode())
        raw = self._transport.receive(timeout=2.0).decode().strip()
        if "=" not in raw:
            raise ValueError(
                f"台架应答里没有 量名=值,读不回三相设定: {raw!r}"
                f"(mode: simulator 时 connection 要写 simulated_grid: true)"
            )
        applied = dict(pair.split("=", 1) for pair in raw.split())
        return self._grid_simulator.apply_three_phase(
            {name: float(value) for name, value in applied.items()}
        )

    def apply_initial_setpoint(self, setpoint: Mapping[str, float]) -> GridSetpoint:
        """把电网设回清单声明的初始设定(用例收尾 / teardown 复位用)。

        设定项名就是设定值对象的字段名:`{voltage_v: 230, freq_hz: 50}`。组合根不认识
        "电压/频率",它只按 SetpointRestorableDriver 这个接口把清单里的映射原样交进来。
        """
        return self.apply_grid(float(setpoint["voltage_v"]), float(setpoint["freq_hz"]))

    @property
    def device(self) -> GridSimulator:
        return self._grid_simulator


@register_device("grid_simulator", "vendor_ascii")
def _build_grid_simulator(
    device_id: str,
    spec: dict,
    model: Model,
    transport: ByteStreamTransport,
    criteria: CriteriaProfile,
) -> GridSimulatorDriver:
    """周边设备不判合格,criteria 收下但不用——它和被测设备共用同一个工厂签名。"""
    return GridSimulatorDriver(
        GridSimulator(device_id=device_id, name=spec.get("name", device_id)), transport
    )
