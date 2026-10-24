"""
工控机网关透传 Transport。

典型场景:某些设备(比如挂在产线工控机本地 CAN/串口总线上的逆变器)
测试平台没法直接网络访问,只能先 SSH 登录到工控机,
再调用工控机本地的一个轻量 agent 命令,把字节转发到本地总线上。

这个文件存在的意义,就是回答"以后要接工控机"这个需求该怎么落地:

    对上层驱动(InverterDriver / BatteryDriver 等)来说,
    GatewaySshTransport 依然只是一个 ByteStreamTransport ——
    只有 connect / disconnect / send / receive 四个方法。
    驱动层完全不知道、也不需要知道"背后多绕了一层 SSH"这件事。

所以新增工控机接入方式时:
    1. 只新增这一个 Transport 实现文件
    2. 在 devices.yaml 里把某个设备的 protocol 改成 gateway_ssh
    domain/ 和 drivers/ 下的任何已有代码都不需要改一行。

这里的实现是最小可运行示例(每次收发都单独起一次 SSH exec_command),
生产环境通常会换成工控机上常驻一个 TCP agent 服务来减少握手开销,
但那只是这个类内部实现细节的替换,接口形状不变,上层代码同样不受影响。

两个失败语义的约定(它们决定上层把结论归到谁头上):
- 断链(`ConnectionError`、证据记 `error: closed`):SSH 会话本身断了、或 agent 返回非零。
  两者都归为“链路不在”,今天不再拆类型:要分开得先让 agent 返回结构化错误码
  (今天只有退出码 + stderr),而两种情况下上层结论一样 —— 台架链路不在 = 这次测试无效。
  已知失真:agent 自己等超时也是非零退出,所以“总线上的设备不回”在这一路径会报成
  `closed` 而不是 `timeout`。分不开就不猜,agent 的 stderr 留在消息里可查;
  等 agent 能给出“没人答”与“链路不在”两种错误码时再把这一对拆开。
- 短帧(`IncompleteFrameError`、证据记 `error: timeout`):agent 回了字节但不够一帧,
  半帧绝不交给上层。
"""
from __future__ import annotations

import base64
import shlex

from .interfaces import ByteStreamTransport, FrameLength, IncompleteFrameError, expected_bytes
from .registry import register_transport


class GatewaySshTransport:
    def __init__(
        self,
        gateway_host: str,
        gateway_user: str,
        local_bus: str,
        key_path: str | None = None,
        known_hosts: str | None = None,
    ) -> None:
        self._gateway_host = gateway_host
        self._gateway_user = gateway_user
        self._local_bus = local_bus
        self._key_path = key_path
        self._known_hosts = known_hosts
        self._ssh = None
        self._connected = False

    def connect(self) -> None:
        # 复用 real_transports 里已经写好的 SSH 实现,而不是重新造一个 SSH 客户端——
        # 这也是"消费者定义接口"带来的好处:RealSshTransport 本身就满足 SshTransport,
        # 这里直接把它当成一个内部零件使用。
        from .real_transports import RealSshTransport

        self._ssh = RealSshTransport(
            host=self._gateway_host,
            user=self._gateway_user,
            key_path=self._key_path,
            known_hosts=self._known_hosts,
        )
        self._ssh.connect()
        self._connected = True

    def disconnect(self) -> None:
        if self._ssh is not None:
            self._ssh.disconnect()
        self._connected = False

    def has_client(self) -> bool:
        return self._connected

    def send(self, data: bytes) -> None:
        payload = base64.b64encode(data).decode()
        # local_bus 在装配期已过字符集白名单,这里的 quote 是第二道防线:万一有形态混过白名单,
        # 也只会当成一个字符串交给 bus-agent,不会被 ssh 那头的 shell 解释。白名单字符集全在
        # shlex 的安全集内,合法值的命令串与不 quote 时逐字节相同。
        cmd = f"bus-agent send --bus {shlex.quote(self._local_bus)} --data {shlex.quote(payload)}"
        result = self._ssh.exec_command(cmd)
        if result.exit_code != 0:
            # 链路不在(语义见模块文档):沿用 closed,不与"对端 FIN"分类型。
            raise ConnectionError(f"网关转发发送失败: {result.stderr}")

    def receive(self, timeout: float = 2.0, expected: FrameLength = None) -> bytes:
        cmd = f"bus-agent recv --bus {shlex.quote(self._local_bus)} --timeout {timeout}"
        result = self._ssh.exec_command(cmd, timeout=timeout + 2)
        if result.exit_code != 0:
            # 同上:接口不可用就是链路不在,归到 closed。
            raise ConnectionError(f"网关转发接收失败: {result.stderr}")
        data = base64.b64decode(result.stdout.strip())
        # agent 只转回它当时收到的字节:不够一帧就报错,不把半帧交给上层。
        total = expected_bytes(expected, data)
        if total is not None and len(data) < total:
            raise IncompleteFrameError(
                f"网关 {self._local_bus}: 要一帧 {total} 字节,agent 只转回 {len(data)} 字节"
            )
        return data


@register_transport("gateway_ssh", "real")
def _build_gateway_ssh(cfg: dict) -> ByteStreamTransport:
    return GatewaySshTransport(
        gateway_host=cfg["gateway_host"],
        gateway_user=cfg["gateway_user"],
        local_bus=cfg["local_bus"],
        key_path=cfg.get("gateway_key_path"),
        known_hosts=cfg.get("gateway_known_hosts"),
    )
