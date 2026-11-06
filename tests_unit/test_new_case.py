"""new-case:往已有项目加一条用例,不重新生成整个项目。"""
import pytest

from testplatform import scaffold
from testplatform.cli import main


def _project(tmp_path):
    return scaffold.scaffold("case", tmp_path / "proj")


def test_new_case_writes_a_template_next_to_the_existing_cases(tmp_path):
    project = _project(tmp_path)

    path = scaffold.new_case("voltage_is_positive", project)

    text = path.read_text(encoding="utf-8")
    assert path.name == "test_voltage_is_positive.py"
    assert "def test_voltage_is_positive(composition_root)" in text
    assert "@case(id='voltage_is_positive'" in text
    assert "SCAFFOLD-1" in text
    assert "composition_root.get_ready('dev_01')" in text
    assert "assert reading.readings" in text
    assert "# assert reading.passed" in text


def test_new_case_refuses_to_overwrite_an_existing_file(tmp_path):
    project = _project(tmp_path)
    scaffold.new_case("voltage", project)

    with pytest.raises(FileExistsError, match="已存在"):
        scaffold.new_case("voltage", project)


def test_new_case_refuses_an_unknown_clause_and_lists_the_known_ones(tmp_path):
    project = _project(tmp_path)
    unknown = "NO" + "-SUCH"

    with pytest.raises(ValueError, match="条款") as caught:
        scaffold.new_case("voltage", project, clause=unknown)

    assert "SCAFFOLD-1" in str(caught.value)


def test_new_case_requires_a_scaffolded_project(tmp_path):
    with pytest.raises(ValueError, match="scaffold"):
        scaffold.new_case("voltage", tmp_path)


def test_cli_new_case_writes_the_file(tmp_path, capsys):
    project = _project(tmp_path)

    assert main(["new-case", "voltage", "--out", str(project)]) == 0

    assert (project / "tests" / "test_voltage.py").exists()
    assert "已生成" in capsys.readouterr().out


def test_new_case_allows_a_free_clause_without_a_clause_registry(tmp_path):
    """EdgeC 的 acceptance 产品项目只有 devices.yaml;free: 条款要能用。"""
    project = _project(tmp_path)
    (project / "configs" / "clauses.yaml").unlink()

    path = scaffold.new_case("issue_123", project, clause="free: EdgeC issue #123")

    assert "clause='free: EdgeC issue #123'" in path.read_text(encoding="utf-8")


def test_new_case_quotes_free_text_and_rejects_an_unknown_device(tmp_path):
    project = _project(tmp_path)
    (project / "configs" / "clauses.yaml").unlink()

    path = scaffold.new_case(
        "quoted", project, clause='free: 用户说"电压要稳"\n第二行'
    )
    compile(path.read_text(encoding="utf-8"), str(path), "exec")

    with pytest.raises(ValueError, match="设备"):
        scaffold.new_case("unknown", project, device="not_a_device")


def test_generated_case_calls_get_ready_and_does_not_require_a_verdict(tmp_path):
    project = _project(tmp_path)
    path = scaffold.new_case("plain", project)
    namespace = {"__name__": "generated_case"}
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace)

    class Device:
        def __init__(self):
            self.readings = {"电压": 230.0}

        def connect(self):
            return None

        def read_all(self):
            return self

    class Root:
        def __init__(self):
            self.requested = []

        def get_ready(self, device_id):
            self.requested.append(device_id)
            return Device()

    root = Root()
    namespace["test_plain"](root)

    assert root.requested == ["dev_01"]


def test_new_case_without_a_clause_registry_requires_free(tmp_path):
    project = _project(tmp_path)
    (project / "configs" / "clauses.yaml").unlink()

    with pytest.raises(ValueError, match="free:"):
        scaffold.new_case("issue_123", project)
