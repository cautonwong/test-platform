"""协议注册表:协议名 → 允许的接入方式。

内置规约在 `config.manifest.PROTOCOL_ACCESS` 里留一份静态表,供"只解析清单、不 import 驱动"
的独立调用使用。驱动模块再调用 `register_protocol` 声明自己的规约;组合根装配前这些声明
已经生效 —— 新增规约因此不需要再改平台源码里的中央表。

注册是幂等的:同一个协议用同一组接入方式注册多次(多个驱动共用一种规约)不报错;
用不同的接入方式重复注册会报错,避免两个驱动对同一种规约各说一套。
"""
from __future__ import annotations

from typing import Dict, FrozenSet

_ACCESS: Dict[str, FrozenSet[str]] = {}


def register_protocol(name: str, *, access) -> None:
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"协议名必须是非空字符串,拿到 {name!r}")
    declared = frozenset(access)
    if not declared:
        raise ValueError(f"协议 {name!r} 必须至少声明一种接入方式")
    existing = _ACCESS.get(name)
    if existing is not None:
        if existing != declared:
            raise ValueError(
                f"协议 {name!r} 重复注册且接入方式不一致: "
                f"{sorted(existing)} vs {sorted(declared)}"
            )
        return
    _ACCESS[name] = declared


def access_of(name: str) -> FrozenSet[str] | None:
    return _ACCESS.get(name)


def registered_protocols() -> list[str]:
    return sorted(_ACCESS)
