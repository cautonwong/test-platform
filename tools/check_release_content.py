"""
发布前的包内容检查(票 09g):包里不许出现真实客户条目 / 真实点表 / 客户名称。

    python tools/check_release_content.py npm      # 检查待发布的 npm 包目录

退出码 0 = 干净;1 = 命中(消息里指出命中了什么、在哪个文件)。发布 job 直接拿它当闸门。
为什么要有:轮子随包分发 `_data/`,而 `_data/` 里放什么完全取决于打包的人 ——
把一份客户的 devices.yaml 顺手带进去,不会有任何测试红,但一旦发布就收不回来了。
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

# 真实客户数据的痕迹:客户名称 / 合同编号 / 真实设备清单文件名。
# 这不是"敏感词表",而是"打包时最容易被顺手带进去的那几类东西"。
FORBIDDEN = (
    "客户名称",
    "customer_name",
    "contract_no",
    "合同编号",
)
# 轮子里允许带的数据文件:`_data/` 下随包走的就是这几类(执行器资源 + 示例产品 + 嵌套用的 ini)。
# 不在这个名单里的东西一律报红 —— 加东西要**显式**加,不能靠"顺手带进去"。
ALLOWED_DATA = (
    "configs/clauses.yaml",
    "configs/mock_registers.yaml",
    "configs/import/",
    "tools/",
    "tests_support/",
    "pyproject.toml",
)


def scan_package(npm_dir: Path) -> list[str]:
    findings: list[str] = []
    for wheel in (npm_dir / "dist").glob("*.whl"):
        with zipfile.ZipFile(wheel) as archive:
            for name in archive.namelist():
                if name.endswith("/"):
                    continue
                if "testplatform/_data/" in name:
                    relative = name.split("testplatform/_data/", 1)[1]
                    if not any(relative.startswith(prefix) for prefix in ALLOWED_DATA):
                        findings.append(f"{wheel.name}: 包内带了不该随包分发的数据 {relative}")
                text = archive.read(name).decode("utf-8", "ignore")
                for word in FORBIDDEN:
                    if word in text:
                        findings.append(f"{wheel.name}: {name} 含 {word!r}")
    for path in (npm_dir / "cli").glob("*.js"):
        text = path.read_text(encoding="utf-8")
        for word in FORBIDDEN:
            if word in text:
                findings.append(f"{path.name}: 含 {word!r}")
    return findings


def main(argv: list[str] | None = None) -> int:
    target = Path((argv or sys.argv[1:] or ["npm"])[0])
    findings = scan_package(target)
    if findings:
        print("发布前内容检查失败:")
        for finding in findings:
            print(f"  - {finding}")
        return 1
    print(f"发布前内容检查通过:{target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
