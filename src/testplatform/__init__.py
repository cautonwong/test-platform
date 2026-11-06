"""testplatform:多协议、多设备自动化测试平台。

包名只在这里改一处 —— npm 风格的 scope 派生出 PyPI 上的发行版名(发行版名不能带 `@` 和 `/`)。
`pyproject.toml` 的 `name` 由 tests_unit/test_distribution_metadata.py 与这里对齐。
"""

PACKAGE_SCOPE = "@cautonwong"
PACKAGE_NAME = "testplatform"
DISTRIBUTION_NAME = f"{PACKAGE_SCOPE.removeprefix('@')}-{PACKAGE_NAME}"

# 运行时版本号。`pyproject.toml` 的 version 由 tests_unit/test_cli.py 与这里对齐 ——
# 源码布局里没有已安装发行版的元数据,所以不能只靠 importlib.metadata。
__version__ = "0.1.0"
