"""读取 devices.yaml,转成普通 dict(DeviceSpec),交给组合根使用。
这个文件只负责"读文件",不负责"造对象"——造对象是组合根的职责。
"""
from __future__ import annotations

from pathlib import Path

import yaml


def load_device_config(config_path: str | Path) -> dict:
    """整份清单:判据集、型号、设备实例(解析与校验在 config/manifest.py)。"""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"设备配置文件不存在: {path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}
