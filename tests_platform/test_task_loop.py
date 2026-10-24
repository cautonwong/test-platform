"""
任务闭环里被选中的用例:平台与产品读同一台模拟从站,两边读到的东西必须一致。

只在任务执行器下跑:执行器把分配到的端口写进证据目录的 task.json,这条用例照它去连。
默认套件里没有任务在跑(证据目录里没有 task.json),所以跳过并说明原因 —— 不假装跑过,
也不去读出厂清单里写死的端口。

断言的都是外部可观察的东西:平台自己经 Modbus 读到的读数(落成每设备 JSONL)、
产品挂在它监听端口上的那份读数、以及产品读的是不是任务分配给那台表的那条端口。
"""
from __future__ import annotations

import json
import socket

import pytest
import yaml

from testplatform.cases import case
from testplatform.composition.root import CompositionRoot
from testplatform.execution.task import TASK_RECORD_FILE
from tests_support.manifest_factory import manifest

ENERGY = 1234.56
VOLTAGE = 230.0


@case(id="T_00_00_01_000000_01", clause="free: 平台:任务闭环", tags=["任务"])
def test_platform_and_product_read_the_same_simulated_meter(tmp_path, evidence_session):
    ports = _task_slave_ports(evidence_session)
    root = _build(tmp_path, ports, evidence_session)
    try:
        measured = {}
        for device_id in ports:
            meter = root.get(device_id)
            meter.connect()
            reading = meter.read_all()
            assert reading.passed is True, f"{device_id} 的读数没通过判据: {reading.evaluation}"
            measured[device_id] = reading.readings
    finally:
        root.teardown()

    product = _ask_product(_task_product_port(evidence_session))
    for device_id, ports_of_device in product["meters"].items():
        assert ports_of_device["port"] == ports[device_id], (
            f"产品读的不是任务分配给 {device_id} 的那条端口:"
            f"产品 {ports_of_device['port']} vs 任务 {ports[device_id]}"
        )
        assert ports_of_device["energy_kwh"] == pytest.approx(measured[device_id]["有功电量"]), (
            f"产品经 Modbus 读到 {ports_of_device['energy_kwh']} kWh,"
            f"平台读同一台从站是 {measured[device_id]['有功电量']} kWh"
        )
        assert ports_of_device["voltage_v"] == pytest.approx(measured[device_id]["电压"])


def _task_slave_ports(evidence_session) -> dict[str, int]:
    ports = _task_record(evidence_session)["slave_ports"]
    if not ports:
        pytest.skip("这一轮任务没有拉起模拟从站(launch 里没有 ${SLAVE_PORT:<设备id>})")
    return ports


def _task_product_port(evidence_session) -> int:
    return int(_task_record(evidence_session)["product_port"])


def _task_record(evidence_session) -> dict:
    path = evidence_session.directory / TASK_RECORD_FILE
    if not path.exists():
        pytest.skip("这条用例只在任务执行器下跑(证据目录里没有 task.json;python tools/run_task.py <任务文件>)")
    return json.loads(path.read_text(encoding="utf-8"))


def _build(tmp_path, ports: dict[str, int], evidence):
    """一份现造的清单:每台表按任务分配给它的那条端口走真实 socket(RTU 帧,不是 MBAP)。

    从站地址也按任务记录来:任务给每台模拟从站的是哪条地址,平台就问哪条 —— 两边必须是同一台表。
    """
    addresses = _task_record(evidence)["slave_addresses"]

    def _real_socket(config):
        for spec in config["devices"]:
            spec.update(
                access="tcp",
                mode="real",
                bus_address=addresses[spec["id"]],
                connection={"host": "127.0.0.1", "port": ports[spec["id"]]},
            )

    path = tmp_path / "devices.yaml"
    path.write_text(
        yaml.safe_dump(manifest(meters=list(ports), mutate=_real_socket), allow_unicode=True),
        encoding="utf-8",
    )
    return CompositionRoot.build(str(path), evidence=evidence)


def _ask_product(port: int) -> dict:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        line = connection.makefile("rb").readline()
    if not line:
        raise AssertionError(f"产品在 {port} 上没回任何东西")
    return json.loads(line)
