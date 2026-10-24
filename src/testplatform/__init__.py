"""testplatform:多协议、多设备自动化测试平台。

包名只在这里改一处 —— npm 风格的 scope 派生出 PyPI 上的发行版名(发行版名不能带 `@` 和 `/`)。
`pyproject.toml` 的 `name` 由 tests_unit/test_distribution_metadata.py 与这里对齐。
"""

PACKAGE_SCOPE = "@cautonwong"
PACKAGE_NAME = "testplatform"
DISTRIBUTION_NAME = f"{PACKAGE_SCOPE.removeprefix('@')}-{PACKAGE_NAME}"
