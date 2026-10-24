"""领域层异常。只有这些异常会从聚合根的方法里抛出,
驱动层/应用层通过捕获这些异常来感知"业务规则被违反",
而不是去解析某个协议特有的错误码。
"""


class DeviceNotConnectedError(RuntimeError):
    """在设备未连接时尝试读写。"""


class DeviceFaultError(RuntimeError):
    """设备处于故障态,必须先显式 clear_fault 才能继续操作。"""


class InvariantViolation(ValueError):
    """任何聚合根内部不变量被违反时抛出(数值超出合理范围、状态流转非法等)。"""
