"""
写死 id 的用例取设备:清单里没有这台设备就跳过并说明,不抛 KeyError。

    composition_root.get("inv_01") 找不到时抛 KeyError,读起来像"平台有 bug";
    事实是"这份清单里没有这台设备" —— 换任何客户清单都会这样,与平台无关。

只在这一种情况跳过:点名的设备不在这份清单里。别的异常照旧往上抛:
把任何异常都吞成跳过,等于把真 bug 变成"没跑",那是这个仓库最忌讳的形态。

命名 fixture(tests/conftest.py)与用例体内点名的设备共用这一处:
两份实现必然漂移,跳过理由会一处一套措辞。
"""
from __future__ import annotations

import pytest

# 超过 10 台就只报前 10 台 + 总数:几百台的规模清单里,这句跳过自己会变成一行几 KB 的噪声。
_SHOWN_DEVICES = 10


def device_or_skip(composition_root, device_id: str):
    try:
        return composition_root.get(device_id)
    except KeyError:
        pytest.skip(f"{device_id} 不在这份清单里,{_configured_devices(composition_root)}")


def _configured_devices(composition_root) -> str:
    configured = sorted(composition_root.drivers)
    shown = configured[:_SHOWN_DEVICES]
    more = " …" if len(configured) > len(shown) else ""
    return f"已配置的设备共 {len(configured)} 台: {shown}{more}"
