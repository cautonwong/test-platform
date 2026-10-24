"""
MBAP 事务号的比对:只发不验 = 迟到应答被当成这次请求的读数。

失效场景不是理论的:上一条请求超时、应答迟到;平台发下一条请求,先读到迟到那条。
它来自同一从站、同一功能码、长度也对 —— 单元号 / 功能码 / 字节数三项检查全过,
事务号是唯一能发现它的手段。

所以这一层锁三件事:
1. 对不上就抛 TransactionMismatch(既不是超时、断链,也不是从站拒绝),消息里有期望值与实际值;
2. 对得上照常解析 —— 包括迟到的那一帧是异常应答时:先比事务号再读异常码,不许降级成"从站拒绝";
3. 回卷 65535 → 0 之后 0 是合法的未完成事务号,不能当成"没有未完成请求"。
"""
import pytest

from testplatform.drivers.meter_driver import _MbapLink
from testplatform.protocol import modbus_rtu, modbus_tcp

# 迟到的应答:事务号 999、单元号 1 —— 与当前请求同一个从站、同一功能码、长度也对。
STALE_TRANSACTION = 999
PAYLOAD = bytes.fromhex("0001e240")  # 1234.56 kWh 的寄存器原始字节


def _stale_read_response() -> bytes:
    return modbus_tcp.build_read_response(STALE_TRANSACTION, 1, PAYLOAD)


def _link_with_a_timed_out_request() -> _MbapLink:
    """一个已经超时过一次的链路:发过事务号 1(没人等它),当前未完成的是事务号 2。"""
    link = _MbapLink()
    link.build_read_request(1, 0, 1)  # 事务号 1:超时了
    link.build_read_request(1, 0, 1)  # 事务号 2:当前未完成
    return link


# ===================== 对不上:专属异常,说清期望与实际 =====================

def test_a_late_read_response_is_refused_with_a_named_error():
    """上一条超时、这一条的应答还没到时读到的那一帧:必须红,而且红成第四种现场结论。"""
    with pytest.raises(modbus_tcp.TransactionMismatch) as caught:
        _link_with_a_timed_out_request().parse_read_response(_stale_read_response(), 1)

    message = str(caught.value)
    assert caught.value.expected == 2 and caught.value.actual == STALE_TRANSACTION
    assert "2" in message and str(STALE_TRANSACTION) in message  # 期望值与实际值都在
    assert "迟到" in message  # 并且指名这是上一条请求的迟到应答


def test_a_late_response_is_not_any_of_the_three_known_failures():
    """超时 / 断链 / 从站拒绝各有各的处置:错位应答不许落进其中任何一个分支。

    最要命的是 SlaveExceptionResponse:它触发块读降级拆小重试,而重试拿到的还是错的读数。
    """
    with pytest.raises(modbus_tcp.TransactionMismatch) as caught:
        _link_with_a_timed_out_request().parse_read_response(_stale_read_response(), 1)

    assert not isinstance(
        caught.value, (TimeoutError, ConnectionError, modbus_rtu.SlaveExceptionResponse)
    )


def test_a_late_exception_response_is_not_read_as_a_refusal():
    """迟到的异常应答同样必须按事务号先挡下来:那不是设备拒绝,拆小重试变不出数据。"""
    stale = modbus_tcp.build_exception_response(
        STALE_TRANSACTION, 1, modbus_rtu.ILLEGAL_DATA_ADDRESS
    )

    with pytest.raises(modbus_tcp.TransactionMismatch):
        _link_with_a_timed_out_request().parse_read_response(stale, 1)


@pytest.mark.parametrize(
    "stale, parse",
    [
        pytest.param(
            modbus_tcp.build_write_single_request(STALE_TRANSACTION, 1, 1, 1),
            lambda link, frame: link.parse_write_response(frame, 1, 1),
            id="写单个寄存器",
        ),
        pytest.param(
            modbus_tcp.build_write_multiple_response(STALE_TRANSACTION, 1, 1, 1),
            lambda link, frame: link.parse_write_multiple_response(frame, 1, 1, 1),
            id="写多个寄存器",
        ),
    ],
)
def test_write_echoes_are_checked_against_the_same_transaction(stale, parse):
    """写应答同样是回显:事务号对不上,回显的地址 / 值再像也不是这一问的回答。"""
    link = _link_with_a_timed_out_request()

    with pytest.raises(modbus_tcp.TransactionMismatch):
        parse(link, stale)


# ============================ 对得上:照常解析 ============================

def test_a_matching_transaction_parses_normally():
    """事务号是这一问的:照常交出寄存器字节,不因为加了校验就多一条失败路径。"""
    link = _MbapLink()
    link.build_read_request(1, 0, 2)

    frame = modbus_tcp.build_read_response(1, 1, PAYLOAD)

    assert link.parse_read_response(frame, 1) == PAYLOAD


# ============================= 回卷 65535 → 0 =============================

def test_the_transaction_wraps_to_zero_and_zero_is_a_real_transaction():
    """回卷后 0 是合法的未完成事务号:拿它当真假判据(if not pending)会把校验悄悄关掉。"""
    link = _MbapLink()
    for _ in range(0xFFFF):
        last = link.build_read_request(1, 0, 1)
    assert last[:2] == b"\xff\xff"

    assert link.build_read_request(1, 0, 1)[:2] == b"\x00\x00"
    assert link.parse_read_response(modbus_tcp.build_read_response(0, 1, PAYLOAD), 1) == PAYLOAD
    with pytest.raises(modbus_tcp.TransactionMismatch):
        link.parse_read_response(_stale_read_response(), 1)
