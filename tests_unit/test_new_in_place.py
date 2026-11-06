"""已有项目里加驱动 / 编解码:文件、conftest 导入、entry point 都要一次到位。"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from testplatform import cli, scaffold

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"


def _project(tmp_path):
    return scaffold.scaffold("case", tmp_path / "project")


def test_new_driver_adds_file_conftest_and_entry_point(tmp_path):
    project = _project(tmp_path)

    path = scaffold.new_driver(
        "my_meter", project, device_type="my_meter", protocol="vendor_ascii"
    )

    assert path.name == "my_meter_driver.py"
    assert '@register_device("my_meter", "vendor_ascii")' in path.read_text(encoding="utf-8")
    assert "import my_meter_driver" in (project / "conftest.py").read_text(encoding="utf-8")
    pyproject = (project / "pyproject.toml").read_text(encoding="utf-8")
    assert '[project.entry-points."testplatform.drivers"]' in pyproject
    assert 'my_meter = "my_meter_driver"' in pyproject
    assert 'py-modules = ["my_meter_driver"]' in pyproject


def test_new_driver_refuses_to_overwrite(tmp_path):
    project = _project(tmp_path)
    scaffold.new_driver("my_meter", project)

    with pytest.raises(FileExistsError):
        scaffold.new_driver("my_meter", project)


def test_new_driver_generic_protocol_is_a_skeleton(tmp_path):
    project = _project(tmp_path)

    path = scaffold.new_driver(
        "my_modbus", project, device_type="my_meter", protocol="modbus_rtu"
    )

    text = path.read_text(encoding="utf-8")
    assert '@register_device("my_meter", "modbus_rtu")' in text
    # modbus_rtu 已由平台声明;生成器不能再用 tcp 覆盖成另一组接入方式
    assert "register_protocol(" not in text
    assert "NotImplementedError" in text
    assert "vendor_ascii_reader" not in text


def test_generated_driver_for_an_existing_protocol_imports_with_the_platform(tmp_path):
    project = _project(tmp_path)
    scaffold.new_driver(
        "my_modbus", project, device_type="my_meter", protocol="modbus_rtu"
    )
    env = dict(os.environ, PYTHONPATH=f"{SRC}{os.pathsep}{project}")

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import my_modbus_driver; "
            "import testplatform.drivers.meter_driver; "
            "print('ok')",
        ],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_new_driver_rejects_an_access_the_protocol_does_not_allow(tmp_path):
    project = _project(tmp_path)

    with pytest.raises(ValueError, match="接入方式"):
        scaffold.new_driver("my_645", project, protocol="dlt645")


def test_new_driver_rejects_unsafe_names_before_generating_code(tmp_path):
    project = _project(tmp_path)

    with pytest.raises(ValueError, match="protocol"):
        scaffold.new_driver("unsafe_protocol", project, protocol='bad"name')
    with pytest.raises(ValueError, match="access"):
        scaffold.new_driver("unsafe_access", project, access='bad"name')
    with pytest.raises(ValueError, match="device_type"):
        scaffold.new_driver("unsafe_type", project, device_type='bad"name')


def test_new_simulator_rejects_an_unsafe_access(tmp_path):
    project = _project(tmp_path)

    with pytest.raises(ValueError, match="access"):
        scaffold.new_simulator("unsafe", project, access='bad"name')


def test_new_driver_requires_a_project(tmp_path):
    with pytest.raises(ValueError, match="configs/devices.yaml"):
        scaffold.new_driver("my_meter", tmp_path)


def test_generated_driver_registers_in_a_fresh_interpreter(tmp_path):
    project = _project(tmp_path)
    scaffold.new_driver("my_meter", project, device_type="my_meter", protocol="vendor_ascii")
    env = dict(os.environ, PYTHONPATH=f"{SRC}{os.pathsep}{project}")

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import my_meter_driver; "
            "from testplatform.drivers.registry import registered_keys; "
            "print(('my_meter', 'vendor_ascii') in registered_keys())",
        ],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


def test_new_protocol_self_check_runs(tmp_path):
    project = _project(tmp_path)

    path = scaffold.new_protocol("my_codec", project)

    assert path.name == "my_codec_protocol.py"
    result = subprocess.run(
        [sys.executable, str(path)], cwd=project, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "protocol self-check passed" in result.stdout


def test_new_protocol_refuses_to_overwrite(tmp_path):
    project = _project(tmp_path)
    scaffold.new_protocol("my_codec", project)

    with pytest.raises(FileExistsError):
        scaffold.new_protocol("my_codec", project)


def test_cli_new_driver_and_new_protocol(tmp_path, capsys):
    project = _project(tmp_path)

    assert cli.main(["new-driver", "cli_meter", "--out", str(project)]) == 0
    assert (project / "cli_meter_driver.py").is_file()
    assert cli.main(["new-protocol", "cli_codec", "--out", str(project)]) == 0
    assert (project / "cli_codec_protocol.py").is_file()
    assert "已生成" in capsys.readouterr().out


def test_new_driver_keeps_a_package_layout_installable(tmp_path):
    """包布局项目要把驱动写进 src,否则 entry point 指向 wheel 里不存在的模块。"""
    project = _project(tmp_path)
    (project / "src").mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nname = "pkg-project"\nversion = "0.1.0"\n'
        '[tool.setuptools.packages.find]\nwhere = ["src"]\n',
        encoding="utf-8",
    )

    path = scaffold.new_driver("pkg_meter", project)

    assert path == project / "src" / "pkg_meter_driver.py"
    text = (project / "pyproject.toml").read_text(encoding="utf-8")
    assert 'py-modules = ["pkg_meter_driver"]' in text
    assert 'pkg_meter = "pkg_meter_driver"' in text
    conftest = (project / "conftest.py").read_text(encoding="utf-8")
    assert 'sys.path.insert(0, str(Path(__file__).parent / "src"))' in conftest


def test_two_drivers_are_both_in_py_modules(tmp_path):
    project = _project(tmp_path)

    scaffold.new_driver("first", project)
    scaffold.new_driver("second", project)

    text = (project / "pyproject.toml").read_text(encoding="utf-8")
    assert 'py-modules = ["first_driver", "second_driver"]' in text
    assert 'first = "first_driver"' in text
    assert 'second = "second_driver"' in text


def test_new_simulator_adds_a_transport_entry_point(tmp_path):
    project = _project(tmp_path)

    scaffold.new_simulator("demo", project, access="demo_sim")

    text = (project / "pyproject.toml").read_text(encoding="utf-8")
    assert '[project.entry-points."testplatform.transports"]' in text
    assert 'demo = "demo_simulator"' in text
    assert 'py-modules = ["demo_simulator"]' in text


def test_new_simulator_registers_an_access(tmp_path):
    project = _project(tmp_path)

    path = scaffold.new_simulator("demo", project, access="demo_sim")

    assert path.name == "demo_simulator.py"
    assert '@register_transport("demo_sim", "simulator")' in path.read_text(encoding="utf-8")
    assert "import demo_simulator" in (project / "conftest.py").read_text(encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=f"{SRC}{os.pathsep}{project}")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import demo_simulator; "
            "from testplatform.transport.registry import registered_keys; "
            "print(('demo_sim', 'simulator') in registered_keys())",
        ],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


def test_cli_new_simulator(tmp_path, capsys):
    project = _project(tmp_path)

    assert cli.main(["new-simulator", "cli_sim", "--out", str(project)]) == 0
    assert (project / "cli_sim_simulator.py").is_file()
    assert "已生成" in capsys.readouterr().out


def test_scaffolded_driver_project_carries_an_entry_point(tmp_path):
    project = scaffold.scaffold("driver", tmp_path / "driver_project")

    pyproject = (project / "pyproject.toml").read_text(encoding="utf-8")
    assert '[project.entry-points."testplatform.drivers"]' in pyproject
    assert 'scaffold_meter = "scaffold_meter_driver"' in pyproject


def test_generated_simulator_fault_hooks_fire(tmp_path):
    project = _project(tmp_path)
    scaffold.new_simulator("demo", project, access="demo_sim")
    env = dict(os.environ, PYTHONPATH=f"{SRC}{os.pathsep}{project}")
    script = """
import demo_simulator

for fault, expected in (("timeout", TimeoutError), ("link-lost", ConnectionError)):
    transport = demo_simulator.SimulatedProtocolTransport(
        {"simulated_faults": {fault: True}}
    )
    transport.connect()
    transport.send(b"x")
    try:
        transport.receive()
    except expected:
        print(fault)
    else:
        raise SystemExit(f"{fault} did not fire")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["timeout", "link-lost"]


def test_generated_driver_registers_the_protocol_access(tmp_path):
    project = _project(tmp_path)
    scaffold.new_driver(
        "demo_driver", project, protocol="demo_proto", access="tcp,serial"
    )
    env = dict(os.environ, PYTHONPATH=f"{SRC}{os.pathsep}{project}")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import demo_driver_driver; "
            "from testplatform.config.protocols import access_of; "
            "print(sorted(access_of('demo_proto')))",
        ],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "['serial', 'tcp']"


def test_new_driver_does_not_promise_an_entry_point_on_a_non_setuptools_backend(tmp_path):
    project = _project(tmp_path)
    (project / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n\n'
        '[project]\nname = "hatch-project"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )

    scaffold.new_driver("hatch_meter", project)

    text = (project / "pyproject.toml").read_text(encoding="utf-8")
    assert "entry-points" not in text
    assert "import hatch_meter_driver" in (project / "conftest.py").read_text(encoding="utf-8")
