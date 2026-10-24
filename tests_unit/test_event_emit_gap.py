"""事件没落盘要进报告(票 13a2):报告读 `evidence_gaps`,不是看终端。

三条口径:条数(不是布尔)、干净跑 0 条、没有 round-status 不崩。
"单测注入的假 sink 不许写进真证据会话"由调用点保证(conftest 只在有会话时写),
这里用真实 EvidenceSession 的 API 验它产出的形状。
"""

from __future__ import annotations

import json

from testplatform.reporting.evidence import ROUND_STATUS_FILE, EvidenceSession
from testplatform.reporting.report import _event_emit_gaps


def test_a_real_failure_becomes_a_countable_evidence_gap(tmp_path):
    session = EvidenceSession(tmp_path / "evidence")
    session.record_event_emit_failure("dev_01", missing=3, reason="sink 坏了")
    session.write_round_status()

    assert _event_emit_gaps(session.directory) == [
        {
            "kind": "event_emit_failed",
            "device": "dev_01",
            "missing": 3,  # 条数,不是 True
            "reason": "sink 坏了",
            "ref": ROUND_STATUS_FILE,
        }
    ]
    # 轮次状态跟着标 needs-review:clean 会让复盘的人以为这一轮没问题
    status = json.loads((session.directory / ROUND_STATUS_FILE).read_text(encoding="utf-8"))
    assert status["verdict"] == "needs-review"


def test_a_clean_round_has_no_event_gap(tmp_path):
    session = EvidenceSession(tmp_path / "evidence")
    session.write_round_status()

    assert _event_emit_gaps(session.directory) == []
    status = json.loads((session.directory / ROUND_STATUS_FILE).read_text(encoding="utf-8"))
    assert status["verdict"] == "clean"


def test_a_missing_round_status_is_not_a_crash(tmp_path):
    assert _event_emit_gaps(tmp_path) == []
