"""
资产目录上下文(票 08)。

它是**索引**:从清单、条款清单与测试文件扫描出"有什么、在哪、属于谁",回答六个问题
(型号覆盖 / 条款覆盖 / 设备能跑什么 / 型号 CI 可跑性 / 缺口 / 改判据影响谁)。

它是**派生数据**:不手改,重新生成逐字节一致;`--verify` 跟磁盘比,报多出 / 缺失 / 变了。
它**只读**:不碰报告层与执行层 —— 只需要注册清单(`composition/registrations.py`)来问
"有哪些设备类型、哪个型号有模拟器",不 import 组合根,也不 import `reporting/`、
`execution/`(见 tests_unit/test_catalog_dependencies.py 的导入守卫)。
"""
from testplatform.catalog.catalog import (  # noqa: F401
    CATALOG_DOC,
    CATALOG_FILE,
    Catalog,
    build,
    dumps,
    render,
    scan_cases,
    verify,
    write,
)

__all__ = [
    "CATALOG_DOC",
    "CATALOG_FILE",
    "Catalog",
    "build",
    "dumps",
    "render",
    "scan_cases",
    "verify",
    "write",
]
