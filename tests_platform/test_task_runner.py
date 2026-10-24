"""
任务执行器(票 01)的验收:一份任务文件 → 一条命令跑完 → 退出码 0,证据里能指认实际命令行与端口。

这里断言的都是外部可观察的东西:进程退出码、证据目录里的文件、task.json 的字段、JSONL 里的读数。
不看内部函数调用顺序 —— 那会让"改了实现"变成"改了行为"。

慢启动 / 不启动 / 崩溃三种产品行为各有真进程用例:就绪等待与"任务没跑起来"的区分
只能靠真进程验,不能靠 monkeypatch 掉 Popen。
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from testplatform.execution.task import (
    PORT_ATTEMPTS,
    PORT_RANGE_ENV,
    PORT_RETRY_REASONS,
    TASK_RECORD_FILE,
    TaskFileError,
    TaskRunError,
    load_task,
    run_task,
)
from tests_support.manifest_factory import manifest

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIPPED_TASK = REPO_ROOT / "configs" / "tasks" / "minimal.yaml"
SHIPPED_DEVICES = REPO_ROOT / "configs" / "devices_mock.yaml"
FAKE_PRODUCT = REPO_ROOT / "tests_support" / "fake_product.py"
LOOP_CASE = "T_00_00_01_000000_01"
LOOP_TEST = "test_task_loop.py::test_platform_and_product_read_the_same_simulated_meter"
REAL_PORT_15020 = 15020
# 内联产品(`python -c …`):命令行里没有指向文件的参数,摘要只能说 unknown。
# 行为够跑 PROBE_QUICK_SELECT:监听 ${PORT},未一条 JSON 行,然后等到平台收尾。
INLINE_PRODUCT = (
    "import json, socket, sys, time\n"
    "port = int(sys.argv[sys.argv.index('--port') + 1])\n"
    "server = socket.socket()\n"
    "server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
    "server.bind(('127.0.0.1', port))\n"
    "server.listen()\n"
    "connection, _ = server.accept()\n"
    "connection.sendall((json.dumps({'product': 'fake-edgec'}) + '\\n').encode())\n"
    "connection.close()\n"
    "time.sleep(60)\n"
)
# 死因分类的探针用例(见 tests_platform/test_round_probe_cases.py):01 慢、02 重启、03 快。
# run-scope 的 executed 是节点 id,不是用例编号 —— 断言按测试函数名找。
PROBE_SELECT = "case:T_00_00_03_000000"
PROBE_SLOW_TEST = "test_slow_case_leaves_the_platform_time_to_see_the_product_die"
PROBE_QUICK_SELECT = "case:T_00_00_03_000000_03"
PROBE_RESTART_SELECT = "case:T_00_00_03_000000_02"


# ------------------------------------------------------------ 一条命令跑完 ----

def test_one_command_runs_the_shipped_task_and_records_what_it_actually_ran(tmp_path):
    """票 01 的主验收:写一份任务文件,一条命令跑完,退出码 0,证据里能指认命令行与端口。

    走 `tools/run_task.py` 这个入口(不是直接调 run_task):"一条命令"是验收的一部分。
    """
    evidence = tmp_path / "evidence"
    completed = subprocess.run(
        [sys.executable, "tools/run_task.py", str(SHIPPED_TASK), "--evidence-dir", str(evidence)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=300,
    )
    assert completed.returncode == 0, (
        f"任务没跑通,退出码 {completed.returncode}\n"
        f"stdout:\n{completed.stdout[-4000:]}\nstderr:\n{completed.stderr[-4000:]}"
    )

    # 每设备 JSONL + 轮次范围 + 轮次状态,再加上任务自己的那份事实记录
    for name in (TASK_RECORD_FILE, "run-scope.json", "round-status.json", "meter_01.jsonl"):
        assert (evidence / name).is_file(), f"证据目录缺 {name}: {sorted(p.name for p in evidence.iterdir())}"

    record = _record(evidence)
    # 产品端口由平台分配并真的出现在命令行里 —— 这是"端口填进命令"的唯一证据
    assert record["product_port"] > 0
    assert str(record["product_port"]) in record["launch"], record["launch"]
    assert record["launch"][0] == sys.executable
    # 从站端口也进了实际命令行
    assert record["slave_ports"] == {"meter_01": record["slave_ports"]["meter_01"]}
    assert f"meter_01:{record['slave_ports']['meter_01']}" in record["launch"]
    # 就绪等待真的发生了:产品慢启动 2 秒,任务等到它监听为止
    assert record["readiness"]["seconds"] >= 2.0, record["readiness"]
    assert record["readiness"]["seconds"] < record["readiness"]["timeout"]
    # 这两个端口不是清单里那个写死的 15020:任务是平台分配,不是照抄清单
    assert REAL_PORT_15020 not in (record["product_port"], *record["slave_ports"].values())

    scope = json.loads((evidence / "run-scope.json").read_text(encoding="utf-8"))
    assert any(LOOP_TEST in nodeid for nodeid in scope["executed"]), scope["executed"]
    assert scope["skipped"] == [], f"这条任务不该有跳过的用例: {scope['skipped']}"

    # 证据完整度:实际命令行 / 产品 stdout+stderr / runtime 三项 / env 四项
    for name in ("product.out", "product.err"):
        assert (evidence / name).is_file(), f"证据目录缺 {name}"
    runtime = record["runtime"]
    assert runtime["startup_seconds"] >= 2.0, runtime  # 产品慢启动 2 秒
    assert runtime["first_reading_seconds"] is not None and runtime["first_reading_seconds"] > 0, runtime
    assert runtime["total_seconds"] >= runtime["first_reading_seconds"], runtime
    env = record["env"]
    assert re.fullmatch(r"[0-9a-f]{7,40}", env["commit"] or ""), env
    assert env["platform_version"], env
    assert re.fullmatch(r"[0-9a-f]{64}", env["product_sha256"]), env
    assert re.fullmatch(r"[0-9a-f]{64}", env["simulator_version"] or ""), env

    readings = [
        entry
        for entry in _jsonl(evidence / "meter_01.jsonl")
        if entry["kind"] == "reading"
    ]
    assert readings, "平台自己经真实 socket 读的那条读数必须落进任务证据目录"
    assert readings[0]["readings"]["有功电量"] == pytest.approx(1234.56)


def test_a_product_with_no_file_argument_records_unknown_not_a_missing_field(tmp_path):
    """产品是内联命令(`python -c ...`:命令行里没有指向文件的参数)→ 摘要记 `unknown`,不是 null。

    `null` = 平台没这个字段,`unknown` = 平台不知道是这个产品的哪一份 —— 两个意思必须分开。
    混用会让同一个字段因为"产品怎么写"忽有忽无,读证据的人只能当成装坏了。
    """
    task = _task_file(
        tmp_path,
        devices=SHIPPED_DEVICES,
        kind="smoke",
        launch=[sys.executable, "-c", INLINE_PRODUCT, "--port", "${PORT}"],
        select=PROBE_QUICK_SELECT,
    )
    evidence = tmp_path / "evidence"

    assert run_task(load_task(task), evidence_dir=evidence) == 0

    env = _record(evidence)["env"]
    assert "product_sha256" in env, f"字段本身不许脉席: {env}"
    assert env["product_sha256"] == "unknown", env
    assert re.fullmatch(r"[0-9a-f]{64}", env["simulator_version"] or ""), env


# ------------------------------------------------------- 两块表:两个端口 ----

def test_two_meters_get_two_ports_and_do_not_read_each_other(tmp_path):
    """同一任务两块表 → 两个不同端口,且各自读到自己的值(串了就会有一台读到另一台的数)。

    kind: smoke —— 这条验的是接线(端口 / 读数不串),不是设备的验收结论:选中的用例不按设备展开,
    缺省 acceptance 下的 0/0 会落到「未完成」,那是另一件事。
    """
    devices = _two_meter_devices(tmp_path)
    task = _task_file(
        tmp_path,
        devices=devices,
        kind="smoke",
        launch=[
            sys.executable,
            str(FAKE_PRODUCT),
            "--port",
            "${PORT}",
            "--slave-port",
            "meter_a:${SLAVE_PORT:meter_a}",
            "--slave-port",
            "meter_b:${SLAVE_PORT:meter_b}",
        ],
    )
    evidence = tmp_path / "evidence"
    assert run_task(load_task(task), evidence_dir=evidence) == 0

    record = _record(evidence)
    assert set(record["slave_ports"]) == {"meter_a", "meter_b"}
    assert record["slave_ports"]["meter_a"] != record["slave_ports"]["meter_b"]

    measured = {
        device_id: _first_reading(evidence / f"{device_id}.jsonl")["有功电量"]
        for device_id in ("meter_a", "meter_b")
    }
    assert measured["meter_a"] == pytest.approx(1234.56)
    assert measured["meter_b"] == pytest.approx(500.0)
    # 串了的表现就是两台读到同一个值;这条断言让它红
    assert measured["meter_a"] != measured["meter_b"]


# ------------------------------------------------------------- 加载期报错 ----

def test_placeholder_outside_the_closed_set_fails_at_load_time_and_lists_the_choices(tmp_path):
    """白名单只有 PORT / SLAVE_PORT:<id> / PYTHON。写错名字不能静默留成字面量跑下去。"""
    unknown_name = _task_file(
        tmp_path,
        devices=SHIPPED_DEVICES,
        launch=[sys.executable, str(FAKE_PRODUCT), "--port", "${PORT}", "--slave-port", "${METER_PORT}"],
    )
    with pytest.raises(TaskFileError) as error:
        load_task(unknown_name)
    message = str(error.value)
    assert "METER_PORT" in message and "SLAVE_PORT:<设备id>" in message, message

    unknown_device = _task_file(
        tmp_path,
        devices=SHIPPED_DEVICES,
        launch=[
            sys.executable,
            str(FAKE_PRODUCT),
            "--port",
            "${PORT}",
            "--slave-port",
            "meter_99:${SLAVE_PORT:meter_99}",
        ],
    )
    with pytest.raises(TaskFileError) as error:
        load_task(unknown_device)
    message = str(error.value)
    assert "meter_99" in message and "meter_01" in message, message


def test_missing_manifest_and_missing_product_command_fail_at_load_time(tmp_path):
    """引用不存在的清单 / 不可执行的产品路径 → 加载期报错,并指出是哪一条。"""
    missing_manifest = _task_file(
        tmp_path,
        devices=tmp_path / "no_such_devices.yaml",
        launch=[sys.executable, str(FAKE_PRODUCT), "--port", "${PORT}"],
    )
    with pytest.raises(TaskFileError) as error:
        load_task(missing_manifest)
    assert "devices" in str(error.value) and "no_such_devices.yaml" in str(error.value)

    missing_product = _task_file(
        tmp_path,
        devices=SHIPPED_DEVICES,
        launch=[str(tmp_path / "no_such_product.py"), "--port", "${PORT}"],
    )
    with pytest.raises(TaskFileError) as error:
        load_task(missing_product)
    assert "no_such_product.py" in str(error.value)

    # 解释器 + 脚本的写法同样要在加载期报:漏了它,拼错的脚本名只会变成"产品退出码 2"
    missing_script = _task_file(
        tmp_path,
        devices=SHIPPED_DEVICES,
        launch=[sys.executable, "tests_support/no_such_script.py", "--port", "${PORT}"],
    )
    with pytest.raises(TaskFileError) as error:
        load_task(missing_script)
    assert "no_such_script.py" in str(error.value)

    without_port = _task_file(
        tmp_path, devices=SHIPPED_DEVICES, launch=[sys.executable, str(FAKE_PRODUCT), "--port", "15020"]
    )
    with pytest.raises(TaskFileError) as error:
        load_task(without_port)
    assert "${PORT}" in str(error.value)


# ------------------------------------------------------------ 任务的 kind ----

def test_a_task_file_without_a_kind_field_is_an_acceptance_task(tmp_path):
    """不写 kind = 要验设备(acceptance):缺省是严的那一边,不是冒烟。"""
    without_kind = _task_file(
        tmp_path, devices=SHIPPED_DEVICES, launch=[sys.executable, str(FAKE_PRODUCT), "--port", "${PORT}"]
    )
    assert load_task(without_kind).kind == "acceptance"

    smoke = _task_file(
        tmp_path,
        devices=SHIPPED_DEVICES,
        launch=[sys.executable, str(FAKE_PRODUCT), "--port", "${PORT}"],
        kind="smoke",
    )
    assert load_task(smoke).kind == "smoke"


def test_an_unknown_kind_fails_at_load_time(tmp_path):
    """kind 是闭集:写错不能静默当 acceptance —— 那样冒烟任务的 0/0 会记成"没验到设备"。"""
    bogus = _task_file(
        tmp_path,
        devices=SHIPPED_DEVICES,
        launch=[sys.executable, str(FAKE_PRODUCT), "--port", "${PORT}"],
        kind="bogus",
    )
    with pytest.raises(TaskFileError) as error:
        load_task(bogus)
    message = str(error.value)
    assert "bogus" in message, message
    assert "acceptance" in message and "smoke" in message, message


# ------------------------------------------------- 产品没就绪 / 产品崩溃 ----

def test_product_that_never_listens_is_not_ready_and_the_failure_is_in_evidence(tmp_path):
    """产品没起来 = 平台侧问题(TaskRunError),不是用例失败;证据里留得下这件事。"""
    task = _task_file(
        tmp_path,
        devices=SHIPPED_DEVICES,
        launch=[
            sys.executable,
            str(FAKE_PRODUCT),
            "--port",
            "${PORT}",
            "--startup-delay",
            "30",
        ],
    )
    evidence = tmp_path / "evidence"
    with pytest.raises(TaskRunError) as error:
        run_task(load_task(task), ready_timeout=0.3, evidence_dir=evidence)
    assert "没有监听" in str(error.value), str(error.value)

    record = _record(evidence)
    assert record["readiness"] is None
    assert "没有监听" in record["error"]
    assert record["pytest_exit_code"] is None


def test_crashing_product_is_reported_with_its_exit_code(tmp_path):
    """产品自己退出(崩溃)不能被念成"等超时了":死因要写出来。

    但没到过就绪就没了 = 起不来 = 平台错误:退出码 3 只是事实,**不是死因**,
    证据里不能有 product_exit —— 否则平台问题会被读成产品不合格。
    """
    task = _task_file(
        tmp_path,
        devices=SHIPPED_DEVICES,
        launch=[sys.executable, str(FAKE_PRODUCT), "--port", "${PORT}", "--crash"],
    )
    evidence = tmp_path / "evidence"
    with pytest.raises(TaskRunError) as error:
        run_task(load_task(task), ready_timeout=10, evidence_dir=evidence)
    assert "退出码 3" in str(error.value), str(error.value)
    record = _record(evidence)
    assert record["product_exit_code"] == 3
    assert _events(record, "product_exit") == [], "起不来不是死因:平台错误不能记成产品不合格"
    assert _events(record, "unexecuted") == []


# ------------------------------------------ 死因分类与未执行(真进程) ----

def test_product_that_dies_after_ready_is_a_self_exit_and_leftovers_are_unexecuted(tmp_path):
    """产品已经就绪、用例跑到一半自己没了:这是产品不合格;没跑完的用例记无效,不记通过。

    产品死因与「未执行 N 条」是两条记录,报告要能分开陈述(合起来就只能读到一句"出事了")。
    """
    trigger = tmp_path / "crash.trigger"
    task = _task_file(
        tmp_path,
        devices=SHIPPED_DEVICES,
        launch=[
            sys.executable,
            str(FAKE_PRODUCT),
            "--port",
            "${PORT}",
            "--crash-on-file",
            str(trigger),
        ],
        select=PROBE_SELECT,
    )
    evidence = tmp_path / "evidence"
    # 产品自己退出 → 报告判不合格 → 1(pytest 那一轮是被叫停的 2,不再当结论)
    assert run_task(load_task(task), evidence_dir=evidence) == 1

    record = _record(evidence)
    assert [(event["how"], event["code"], event["signal"]) for event in _events(record, "product_exit")] == [
        ("self_exit", 3, None)
    ], record["process_events"]
    assert _events(record, "unexecuted") == [
        {"kind": "unexecuted", "count": 2, "reason": "product_crashed"}
    ], record["process_events"]
    # 已经跑完的那条保持结论:它在 executed 里,剩下两条一条报告都没打
    scope = _scope(evidence)
    assert any(PROBE_SLOW_TEST in nodeid for nodeid in scope["executed"]), scope
    assert len(scope["collected"]) == 3


def test_platform_teardown_kill_is_not_a_failure_and_leaves_no_unexecuted(tmp_path):
    """平台收尾杀掉跑完的产品:已完成用例结论不变、没有未执行 —— 终止本身不产生结论。"""
    task = _task_file(
        tmp_path,
        devices=SHIPPED_DEVICES,
        kind="smoke",
        launch=[sys.executable, str(FAKE_PRODUCT), "--port", "${PORT}"],
        select=PROBE_QUICK_SELECT,
    )
    evidence = tmp_path / "evidence"
    assert run_task(load_task(task), evidence_dir=evidence) == 0

    record = _record(evidence)
    assert [event["how"] for event in _events(record, "product_exit")] == ["platform_killed"], record[
        "process_events"
    ]
    assert _events(record, "unexecuted") == [], record["process_events"]
    assert len(_scope(evidence)["executed"]) == 1


def test_task_timeout_stops_the_round_and_keeps_what_already_concluded(tmp_path):
    """任务超时:平台主动终止,不计失败;已跑完的保持结论,没跑到的 reason=task_timeout。"""
    task = _task_file(
        tmp_path,
        devices=SHIPPED_DEVICES,
        launch=[sys.executable, str(FAKE_PRODUCT), "--port", "${PORT}"],
        select=PROBE_SELECT,
    )
    evidence = tmp_path / "evidence"
    # 超时叫停 = 没跑完 → 3(未完成);pytest 那一轮是被叫停的 2,不再是退出码
    assert run_task(load_task(task), task_timeout=0.4, evidence_dir=evidence) == 3

    record = _record(evidence)
    scope = _scope(evidence)
    assert any(PROBE_SLOW_TEST in nodeid for nodeid in scope["executed"]), scope
    assert _events(record, "unexecuted") == [
        {"kind": "unexecuted", "count": 2, "reason": "task_timeout"}
    ], record["process_events"]
    # 超时杀的是产品进程(平台发起的终止):没有"产品不合格"这一类结论
    assert [event["how"] for event in _events(record, "product_exit")] == ["platform_killed"]


def test_case_can_restart_the_product_and_the_restart_point_is_in_evidence(tmp_path):
    """需要冷启动的用例要求重启:产品真的换了进程,重启点进证据,旧进程的死不算失败。"""
    task = _task_file(
        tmp_path,
        devices=SHIPPED_DEVICES,
        kind="smoke",
        launch=[sys.executable, str(FAKE_PRODUCT), "--port", "${PORT}"],
        select=PROBE_RESTART_SELECT,
    )
    evidence = tmp_path / "evidence"
    assert run_task(load_task(task), evidence_dir=evidence) == 0

    record = _record(evidence)
    restarts = _events(record, "product_restart")
    assert len(restarts) == 1 and restarts[0]["reason"] == "冷启动", record["process_events"]
    assert [event["how"] for event in _events(record, "product_exit")] == [
        "platform_killed",
        "platform_killed",
    ], record["process_events"]
    assert _events(record, "unexecuted") == []


# ------------------------------------------------- 退出码 = 结论 ----

def test_acceptance_task_that_verified_no_device_exits_3_even_though_pytest_exited_0(tmp_path):
    """报告红、CI 绿是典型假绿:退出码跟着结论走,不跟着 pytest 走。

    这条是守卫:把 run_task 的返回值改回"等于 pytest 的退出码"(这里 pytest 全绿 = 0),
    这条立刻红 —— 报告写着行首「未完成」,进程却回 0。
    """
    task = _task_file(
        tmp_path,
        devices=SHIPPED_DEVICES,
        launch=[sys.executable, str(FAKE_PRODUCT), "--port", "${PORT}"],
    )
    evidence = tmp_path / "evidence"
    assert run_task(load_task(task), evidence_dir=evidence) == 3

    assert _record(evidence)["pytest_exit_code"] == 0, "pytest 这一轮是全绿的,骗不过去"
    summary = json.loads((evidence / "summary.json").read_text(encoding="utf-8"))
    assert (summary["verdict"], summary["reason"]) == ("incomplete", "no_device_coverage"), summary
    assert (summary["kind"], summary["pytest_exit_code"]) == ("acceptance", 0), summary
    first_line = (evidence / "report.md").read_text(encoding="utf-8").splitlines()[0]
    assert first_line.startswith("结论: 未完成(reason: no_device_coverage)"), first_line


def test_a_round_whose_collection_blows_up_exits_2_not_pytests_4(tmp_path):
    """pytest 没跑通(收集期插件报错 / 插件崩)时拿不出结论 → 2;pytest 自己给的 4 只进证据。"""
    task = _task_file(
        tmp_path,
        devices=SHIPPED_DEVICES,
        launch=[sys.executable, str(FAKE_PRODUCT), "--port", "${PORT}"],
        select="case:T_00_00_99_999999_99",
    )
    evidence = tmp_path / "evidence"
    assert run_task(load_task(task), evidence_dir=evidence) == 2

    assert _record(evidence)["pytest_exit_code"] == 4


def test_a_product_that_never_listens_exits_4(tmp_path):
    """起不来 = 平台错误 → 4(走 `tools/run_task.py` 这个入口:退出码是它给的)。"""
    task = _task_file(
        tmp_path,
        devices=SHIPPED_DEVICES,
        launch=[
            sys.executable,
            str(FAKE_PRODUCT),
            "--port",
            "${PORT}",
            "--startup-delay",
            "30",
        ],
    )
    evidence = tmp_path / "evidence"
    completed = subprocess.run(
        [
            sys.executable,
            "tools/run_task.py",
            str(task),
            "--evidence-dir",
            str(evidence),
            "--ready-timeout",
            "0.3",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=300,
    )
    assert completed.returncode == 4, (
        f"退出码 {completed.returncode}\nstdout:\n{completed.stdout[-2000:]}\n"
        f"stderr:\n{completed.stderr[-2000:]}"
    )
    assert "任务没跑起来" in completed.stderr, completed.stderr[-2000:]


# ---------------------------------------------- 端口来源与死因留证(票 14) ----

def test_the_product_port_comes_from_the_platform_range(tmp_path, monkeypatch):
    """产品端口从平台自己的段里取,不从系统临时端口池里要。

    守卫:改回"让系统分配一个临时端口"(bind(0)),拿到的端口不会落在这幺窄的段里 —— 这条红。
    """
    monkeypatch.setenv(PORT_RANGE_ENV, "20010-20019")
    evidence = tmp_path / "evidence"
    task = _task_file(
        tmp_path,
        devices=SHIPPED_DEVICES,
        kind="smoke",
        launch=[sys.executable, str(FAKE_PRODUCT), "--port", "${PORT}"],
        select=PROBE_QUICK_SELECT,
    )
    assert run_task(load_task(task), evidence_dir=evidence) == 0

    record = _record(evidence)
    assert record["port_source"] == "platform_range", record["port_source"]
    assert record["port_range"] == "20010-20019", record["port_range"]
    assert record["port_attempts"] == 1 + len(_events(record, "port_retry")), (
        f"port_attempts 要数全:成功那次 + 之前每一次重试 {record['port_attempts']}"
    )
    port = record["product_port"]
    assert 20010 <= port <= 20019, (
        f"端口 {port} 不在平台自己的段 {record['port_range']} 里:端口又被系统分配了"
    )
    assert str(port) in record["launch"], record["launch"]


def test_a_fully_occupied_port_range_is_a_platform_error(tmp_path, monkeypatch):
    """整段都 bind 不上 = 平台错误(不是产品不合格),消息里点明段值、试过几个、端口来源。

    守卫:删掉"整段不可用"那个报错、让它静默回落成 bind(0) —— 这里就变成能跑起来,不再红。
    """
    monkeypatch.setenv(PORT_RANGE_ENV, "20020-20021")
    held = []
    for port in (20020, 20021):
        probe = socket.socket()
        probe.bind(("127.0.0.1", port))
        probe.listen(1)
        held.append(probe)
    try:
        evidence = tmp_path / "evidence"
        task = _task_file(
            tmp_path,
            devices=SHIPPED_DEVICES,
            launch=[sys.executable, str(FAKE_PRODUCT), "--port", "${PORT}"],
        )
        with pytest.raises(TaskRunError) as error:
            run_task(load_task(task), evidence_dir=evidence)

        message = str(error.value)
        assert "不是产品不合格" in message, message
        assert "20020-20021" in message, message
        assert "port_attempts=2" in message and "port_source=platform_range" in message, message

        record = _record(evidence)
        assert record["port_attempts"] == 2, record["port_attempts"]
        assert record["port_source"] == "platform_range", record["port_source"]
        assert record["port_range"] == "20020-20021", record["port_range"]
        assert record["product_port"] is None, "整段不可用时一个端口都没拿到,不许拿别人的端口凑数"
        assert _events(record, "product_exit") == [], "端口不可用不是产品的死因"
    finally:
        for probe in held:
            probe.close()


def test_a_port_range_overlapping_the_os_allocator_fails_before_the_task_starts(
    tmp_path, monkeypatch
):
    """可配置项不能把保护关掉:段与系统分配段重叠 → 开跑前就报,不是跑起来撞上才报。

    守卫:删掉重叠校验 —— 这里不再抛错,用例红。
    """
    monkeypatch.setenv(PORT_RANGE_ENV, "50000-51000")
    evidence = tmp_path / "evidence"
    task = _task_file(
        tmp_path,
        devices=SHIPPED_DEVICES,
        launch=[sys.executable, str(FAKE_PRODUCT), "--port", "${PORT}"],
    )
    with pytest.raises(TaskRunError) as error:
        run_task(load_task(task), evidence_dir=evidence)

    message = str(error.value)
    assert "重叠" in message and "49152" in message, message
    assert "50000-51000" in message, message
    assert not evidence.exists(), "报错要在开跑前:从站、产品、证据目录都不该动过"


# 每次启动都自报 bind 失败、然后退出码 1 —— 和 Windows 上丢了端口报 10013 一个样。
BIND_DENIED_PRODUCT = """
import sys

print(
    "PermissionError: [WinError 10013] 以一种访问权限不允许的方式做了一个访问套接字的尝试",
    file=sys.stderr,
    flush=True,
)
sys.exit(1)
"""


def test_a_lost_port_attempt_still_says_what_the_product_did(tmp_path):
    """守卫:端口重试用光时,task.json 里也要读得到产品自己的退出码与那次尝试的死因。

    变异:去掉 _abandon_product 里保留退出码那一行 → product_exit_code 变 null,这条红。

    how=unverified 读成"**平台没拿到死因**"(它没到过就绪,下不了结论),**不是**"产品没崩":
    产品确实退出了,退出码就在旁边的 code 里。
    """
    record = _round_with_a_bind_denied_product(tmp_path / "denied")
    assert record["port_attempts"] == PORT_ATTEMPTS, record["port_attempts"]
    assert record["product_exit_code"] == 1, (
        f"产品自己退出码 1,记录里不许是 null: {record['product_exit_code']!r}"
    )
    retries = _events(record, "port_retry")
    assert [event["abandoned_product"]["code"] for event in retries] == [1] * (
        PORT_ATTEMPTS - 1
    ), retries
    assert {event["abandoned_product"]["how"] for event in retries} == {"unverified"}, retries
    _death_causes_are_named(record)


def test_no_exception_path_leaves_an_unnamed_death(tmp_path, monkeypatch):
    """守卫:产品起不来 / 崩溃 / 端口全丢 / 端口被抢这些异常路径上,task.json 里不许有没名字的死因。

    变异:让 attempt_exit 的 how 变 None(或去掉那条保留退出码的修复)→ 这条红。
    """
    slow = tmp_path / "slow"
    slow.mkdir()
    task = _task_file(
        slow,
        devices=SHIPPED_DEVICES,
        launch=[
            sys.executable,
            str(FAKE_PRODUCT),
            "--port",
            "${PORT}",
            "--startup-delay",
            "30",
        ],
    )
    with pytest.raises(TaskRunError):
        run_task(load_task(task), ready_timeout=0.3, evidence_dir=slow / "evidence")
    _death_causes_are_named(_record(slow / "evidence"))

    crashed = tmp_path / "crashed"
    crashed.mkdir()
    task = _task_file(
        crashed,
        devices=SHIPPED_DEVICES,
        launch=[sys.executable, str(FAKE_PRODUCT), "--port", "${PORT}", "--crash"],
    )
    with pytest.raises(TaskRunError):
        run_task(load_task(task), evidence_dir=crashed / "evidence")
    record = _record(crashed / "evidence")
    assert record["product_exit_code"] == 3, record["product_exit_code"]
    _death_causes_are_named(record)

    _death_causes_are_named(_round_with_a_bind_denied_product(tmp_path / "denied"))

    monkeypatch.setenv(PORT_RANGE_ENV, "20040-20041")
    held = []
    for port in (20040, 20041):
        probe = socket.socket()
        probe.bind(("127.0.0.1", port))
        probe.listen(1)
        held.append(probe)
    try:
        lost = tmp_path / "lost"
        lost.mkdir()
        task = _task_file(
            lost,
            devices=SHIPPED_DEVICES,
            launch=[sys.executable, str(FAKE_PRODUCT), "--port", "${PORT}"],
        )
        with pytest.raises(TaskRunError):
            run_task(load_task(task), evidence_dir=lost / "evidence")
        _death_causes_are_named(_record(lost / "evidence"))
    finally:
        for probe in held:
            probe.close()


def test_a_crashed_product_is_not_retried_even_when_the_port_looks_unavailable(tmp_path):
    """守卫:产品自己死了(退出码 3),端口又刚好查着"别人占着" —— 也不许换端口重来。

    判序写死:先看产品死活,再看端口能不能用。倒过来(先问端口)就会把"产品崩了"读成"端口被抢",
    白重试一次(实测到的那条红:port_retry 里多一条 attempt=2)。
    变异:去掉 _port_trouble_reason 里"已经退出就不重试"那一步 → 这条红。
    """
    # 只 bind 不 listen:别的进程 bind 不上(端口查着不可用),但连接也连不上(就绪等待不会被它骗过)。
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    taken = holder.getsockname()[1]

    def reserve():
        # 平台只会把分配器给的套接字关掉;真正占着 taken 的是 holder(所以端口查着不可用)。
        side = socket.socket()
        side.bind(("127.0.0.1", 0))
        return side, taken

    try:
        evidence = tmp_path / "evidence"
        task = _task_file(
            tmp_path,
            devices=SHIPPED_DEVICES,
            launch=[sys.executable, str(FAKE_PRODUCT), "--port", "${PORT}", "--crash"],
        )
        with pytest.raises(TaskRunError) as error:
            run_task(load_task(task), evidence_dir=evidence, port_reserver=reserve)

        message = str(error.value)
        assert "退出码 3" in message, message
        assert "平台错误" not in message, message

        record = _record(evidence)
        assert _events(record, "port_retry") == [], "产品自己崩了不是端口问题,不许重试"
        assert record["port_attempts"] == 1, record["port_attempts"]
        assert record["product_exit_code"] == 3, record["product_exit_code"]
    finally:
        holder.close()


def test_every_port_retry_says_why_it_retried(tmp_path):
    """port_retry.reason 只能取两个值,且各自写的是依据 —— 不许出现"因为 bind 成功所以重试"这种反因果。

    变异:把 reason 改成自由文本(比如 "bind 成功了")→ 这条红。
    这里钉的是**证据里那两个字面值**(人读的那份契约),不是常量:常量与字面值一起改才会骗过断言。
    """
    assert PORT_RETRY_REASONS == ("端口他人不可用", "产品 stderr 自报 bind 失败"), PORT_RETRY_REASONS
    record = _round_with_a_bind_denied_product(tmp_path / "reported")
    reasons = [event["reason"] for event in _events(record, "port_retry")]
    assert reasons == ["产品 stderr 自报 bind 失败"] * (PORT_ATTEMPTS - 1), reasons
    assert set(reasons) <= set(PORT_RETRY_REASONS), reasons

    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    taken = holder.getsockname()[1]
    handed: list[int] = []

    def reserve():
        if not handed:
            handed.append(taken)
            return None, taken
        # 备用端口从夹具私有段里拿(19000+,pid 错开、探针带 SO_REUSEADDR):
        # 不从系统临时池 bind(0)—— 产品带 SO_REUSEADDR,去 bind 我们刚放开的系统端口会拿到 10013,
        # 那是夹具自己制造的故障,会让这条守卫红在一个与被测行为无关的地方。
        for offset in range(32):
            candidate = 19000 + os.getpid() % 64 + offset
            side = socket.socket()
            side.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                side.bind(("127.0.0.1", candidate))
            except OSError:
                side.close()
                continue
            return side, candidate
        raise AssertionError("夹具备用端口段都被占了")

    try:
        directory = tmp_path / "taken"
        directory.mkdir()
        task = _task_file(
            directory,
            devices=SHIPPED_DEVICES,
            kind="smoke",
            launch=[sys.executable, str(FAKE_PRODUCT), "--port", "${PORT}"],
            select=PROBE_QUICK_SELECT,
        )
        assert run_task(load_task(task), evidence_dir=directory / "ev", port_reserver=reserve) == 0
        reasons = [event["reason"] for event in _events(_record(directory / "ev"), "port_retry")]
        # 第一条必定是"端口他人不可用";后面若还有,只能是产品自己报 bind 失败(注入的端口在系统池里)。
        assert reasons[0] == "端口他人不可用", reasons
        assert set(reasons) <= set(PORT_RETRY_REASONS), reasons
    finally:
        holder.close()


def _round_with_a_bind_denied_product(directory: Path) -> dict:
    """跑一轮"产品每次都说 bind 失败"的任务:三次尝试全丢端口 → 返回落盘的 task.json。"""
    directory.mkdir()
    product = directory / "bind_denied.py"
    product.write_text(BIND_DENIED_PRODUCT, encoding="utf-8")
    task = _task_file(
        directory,
        devices=SHIPPED_DEVICES,
        launch=[sys.executable, str(product), "--port", "${PORT}"],
    )
    with pytest.raises(TaskRunError) as error:
        run_task(load_task(task), evidence_dir=directory / "evidence")
    assert "不是产品不合格" in str(error.value), str(error.value)
    return _record(directory / "evidence")


# ------------------------------------------------------------------ 工具 ----

def _task_file(
    tmp_path: Path,
    *,
    devices,
    launch: list[str],
    select: str = f"case:{LOOP_CASE}",
    kind: str | None = None,
):
    path = tmp_path / "task.yaml"
    written = {
        "id": "probe",
        "product": "fake-edgec",
        "launch": launch,
        "devices": str(devices),
        "select": select,
    }
    if kind is not None:
        written["kind"] = kind
    path.write_text(
        yaml.safe_dump(written, allow_unicode=True),
        encoding="utf-8",
    )
    return path


def _two_meter_devices(tmp_path: Path) -> Path:
    """两台表的寄存器初值不同(1234.56 / 500.0 kWh):串了才看得出来。"""

    def _distinct_registers(config):
        for spec, (high, low) in zip(config["devices"], ((0x0001, 0xE240), (0x0000, 0xC350))):
            # 两条独立总线,各自只挂一个从站地址 1:物理上是两根串口 / 两个串口服务器端口
            spec["bus_address"] = 1
            spec["connection"] = {"simulated_registers": {0: high, 1: low, 2: 2300}}

    path = tmp_path / "devices_two.yaml"
    path.write_text(
        yaml.safe_dump(
            manifest(meters=["meter_a", "meter_b"], mutate=_distinct_registers),
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return path


def _record(directory: Path) -> dict:
    path = directory / TASK_RECORD_FILE
    assert path.is_file(), f"没有任务事实记录: {sorted(p.name for p in directory.iterdir())}"
    return json.loads(path.read_text(encoding="utf-8"))


def _events(record: dict, kind: str) -> list[dict]:
    return [event for event in record.get("process_events", []) if event["kind"] == kind]


def _death_causes_are_named(record: dict) -> None:
    """异常路径上不许出现没名字的死因:product_exit 要有 how / code|signal,被放弃的尝试要有 how。"""
    for event in _events(record, "product_exit"):
        assert event["how"] in ("self_exit", "platform_killed"), event
        assert event["code"] is not None or event["signal"] is not None, event
    for event in _events(record, "port_retry"):
        abandoned = event.get("abandoned_product")
        if abandoned is None:
            continue  # 这一次尝试没起产品:没有进程事实可记
        assert abandoned["how"] is not None, event


def _scope(directory: Path) -> dict:
    return json.loads((directory / "run-scope.json").read_text(encoding="utf-8"))


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _first_reading(path: Path) -> dict:
    readings = [entry for entry in _jsonl(path) if entry["kind"] == "reading"]
    assert readings, f"{path.name} 里没有读数"
    return readings[0]["readings"]


def test_platform_stop_keeps_the_verdict_even_when_pytest_exits_4(tmp_path):
    """被平台叫停的轮次 pytest 也会给非 0/1(叫停 = 中断,不是崩)。

    stop_reason 非空 → 走 verdict;写反成「非 0/1 一律记测试无效」,这条红。
    """
    from testplatform.execution.process_control import request_stop
    from testplatform.execution.task import VERDICT_FAILED, _exit_code

    request_stop(tmp_path, "task_timeout")
    assert _exit_code(VERDICT_FAILED, 4, tmp_path) == 1


def test_pytest_blowing_up_without_a_stop_is_invalid_not_the_verdict(tmp_path):
    """没有平台叫停记录时,非 0/1 说明本轮压根没跑出结论:归「测试无效」(2)。

    和上一条互为反例:去掉 `stop_reason(...) is None` 这个条件,这条红。
    """
    from testplatform.execution.task import VERDICT_FAILED, _exit_code

    assert _exit_code(VERDICT_FAILED, 4, tmp_path) == 2
