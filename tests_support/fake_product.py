"""
假产品 —— 用一个小脚本扮演"被测产品"(EdgeC 网关),把任务执行器跑通,不依赖 EdgeC 编译。

它做产品在生产里最少要做的那几件事:
    * 监听一个 TCP 端口(端口由平台分配、经 ${PORT} 传进来),端口起来的那一刻就是"就绪"
    * 经 Modbus RTU 或 MBAP 从模拟从站读表(帧格式由 --frame 选,缺省 RTU;
      从站端口由平台分配、经 ${SLAVE_PORT:<设备id>} 传进来)
    * 把读到的东西挂在监听端口上(一条 JSON 行),平台侧用例据此断言"产品读到了什么"
    * 可配置慢启动(--startup-delay 秒)与崩溃(--crash):就绪等待与死因分类需要真进程才能验

    python tests_support/fake_product.py --port 5000 --slave-port meter_01:5001
    python tests_support/fake_product.py --port 5000 --slave-port meter_01:5001 --startup-delay 2
    python tests_support/fake_product.py --port 5000 --slave-port meter_tcp_01:5001 --frame modbus_tcp
    python tests_support/fake_product.py --port 5000 --slave-port meter_01:5001 --crash

--crash-on-file 是"跑着跑着死掉":某个文件一旦出现就退出码 3。死因分类的用例靠它造出
"产品已经就绪、用例跑到一半没了",不依赖睡多久这种猜得出来的时间。

不模拟设备业务行为(电量累积、状态机、告警时序)。
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from testplatform.protocol import modbus_rtu, modbus_tcp  # noqa: E402

# 点表:0–1 是有功电量(两个寄存器,倍率 0.01 kWh),2 是电压(倍率 0.1 V)。
# 和 configs/mock_registers.yaml / configs/devices.yaml 的 dtsd1352_modbus 一致 ——
# 产品的读法不认地址,只认这份点表;换型号是换脚本,不是换平台。
ENERGY_ADDRESS, ENERGY_BYTES, ENERGY_SCALE = 0, 4, 0.01
VOLTAGE_ADDRESS, VOLTAGE_BYTES, VOLTAGE_SCALE = 2, 2, 0.1
READ_COUNT = 3
# MBAP 帧里的事务号:这个产品一条连接只发一条请求,常量就够(平台侧按请求自己递增)。
MBAP_TRANSACTION = 1
FRAMES = ("modbus_rtu", "modbus_tcp")


def _read_request(frames: str, slave: int) -> bytes:
    """一次读请求的字节。两种帧的 PDU 逐字节相同,差别只在 MBAP 那 7 字节头。"""
    if frames == "modbus_tcp":
        return modbus_tcp.build_read_request(MBAP_TRANSACTION, slave, ENERGY_ADDRESS, READ_COUNT)
    return modbus_rtu.build_read_request(slave, ENERGY_ADDRESS, READ_COUNT)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="假产品:监听端口 + 经 Modbus 读模拟从站")
    parser.add_argument("--port", type=int, required=True, help="监听端口(平台分配的 ${PORT})")
    parser.add_argument(
        "--slave-port",
        action="append",
        default=[],
        metavar="设备id:端口",
        help="要读的模拟从站,可重复(--slave-port meter_01:5001)",
    )
    parser.add_argument("--slave", type=int, default=1, help="Modbus 从站地址")
    parser.add_argument(
        "--frame",
        choices=FRAMES,
        default="modbus_rtu",
        help="经哪种帧读从站:modbus_rtu(缺省)/ modbus_tcp(带 MBAP 头)",
    )
    parser.add_argument("--startup-delay", type=float, default=0.0, help="启动后等多少秒才开始监听")
    parser.add_argument("--crash", action="store_true", help="启动即崩(退出码 3),用来验死因分类")
    parser.add_argument(
        "--crash-on-file",
        default=None,
        metavar="路径",
        help="这个文件一出现就退出码 3(已经就绪之后),用来验「跑一半产品死了」",
    )
    return parser.parse_args(argv)


def _read_meter(port: int, slave: int, frames: str) -> dict[str, float]:
    request = _read_request(frames, slave)
    # 收帧循环两种格式共用:帧有多长由各自 protocol 模块的 response_length 算。
    response_length = (
        modbus_tcp.response_length if frames == "modbus_tcp" else modbus_rtu.response_length
    )
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        connection.sendall(request)
        response = b""
        expected = None
        while expected is None or len(response) < expected:
            chunk = connection.recv(256)
            if not chunk:
                break
            response += chunk
            expected = response_length(response)
    if frames == "modbus_tcp":
        payload = modbus_tcp.parse_read_response(
            response, slave, expected_transaction=MBAP_TRANSACTION
        )
    else:
        payload = modbus_rtu.parse_read_response(response, slave)
    if len(payload) < ENERGY_BYTES + VOLTAGE_BYTES:
        raise ValueError(f"从站应答的数据段只有 {len(payload)} 字节: {response.hex()}")
    energy = int.from_bytes(payload[:ENERGY_BYTES], "big") * ENERGY_SCALE
    voltage = int.from_bytes(payload[ENERGY_BYTES:ENERGY_BYTES + VOLTAGE_BYTES], "big") * VOLTAGE_SCALE
    return {"energy_kwh": round(energy, 4), "voltage_v": round(voltage, 4)}


def _serve(port: int, state: dict, crash_on_file: Path | None = None) -> int:
    line = json.dumps(state, ensure_ascii=False).encode("utf-8") + b"\n"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen(8)
        listener.settimeout(0.05)  # 没请求的时候也要能看见崩溃触发文件
        # 这一行就是"已就绪"的唯一证据:平台侧等的是端口能连上,不是 stdout 里有字。
        print(f"假产品监听 127.0.0.1:{port}", flush=True)
        while True:
            if crash_on_file is not None and crash_on_file.exists():
                print(f"假产品看到 {crash_on_file},主动退出(3)", file=sys.stderr, flush=True)
                return 3
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            with connection:
                connection.sendall(line)


def main(argv: list[str] | None = None) -> int:
    arguments = _parse_args(argv)
    if arguments.crash:
        print("假产品按 --crash 主动退出", file=sys.stderr, flush=True)
        return 3
    if arguments.startup_delay > 0:
        time.sleep(arguments.startup_delay)
    meters = {}
    for written in arguments.slave_port:
        device_id, _, raw_port = written.partition(":")
        meters[device_id] = {
            "port": int(raw_port),
            **_read_meter(int(raw_port), arguments.slave, arguments.frame),
        }
    state = {"product": "fake-edgec", "port": arguments.port, "meters": meters}
    return _serve(
        arguments.port,
        state,
        Path(arguments.crash_on_file) if arguments.crash_on_file else None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
