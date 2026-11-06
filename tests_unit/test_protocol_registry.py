"""协议注册表:新增规约在驱动里声明 access,不改中央表。"""
import pytest

from testplatform.config import protocols
from testplatform.config.manifest import PROTOCOL_ACCESS, parse_manifest
from tests_support.manifest_factory import manifest


def _manifest_with(protocol_name: str, access: str) -> dict:
    def mutate(built: dict) -> None:
        built["models"]["inverter_pv_series"]["protocol"] = protocol_name
        built["devices"][0]["access"] = access

    return manifest(inverters=1, mutate=mutate)


def test_a_registered_protocol_is_accepted_by_the_manifest():
    protocols.register_protocol("fake_proto_c1", access={"tcp"})

    parsed = parse_manifest(_manifest_with("fake_proto_c1", "tcp"))

    assert parsed.models["inverter_pv_series"].protocol == "fake_proto_c1"


def test_a_registered_protocol_still_rejects_an_access_it_did_not_declare():
    protocols.register_protocol("fake_proto_c1_serial", access={"tcp"})

    with pytest.raises(ValueError, match="不能配接入方式"):
        parse_manifest(_manifest_with("fake_proto_c1_serial", "serial"))


def test_re_registering_a_protocol_with_a_different_access_is_rejected():
    protocols.register_protocol("fake_proto_c1_dup", access={"tcp"})

    with pytest.raises(ValueError, match="重复注册"):
        protocols.register_protocol("fake_proto_c1_dup", access={"serial"})


def test_builtin_driver_declarations_agree_with_the_static_table():
    for name, access in PROTOCOL_ACCESS.items():
        registered = protocols.access_of(name)
        if registered is not None:
            assert registered == access, f"{name}: 驱动注册 {registered} 与静态表 {access} 不一致"
