"""
三份 `clauses.yaml` 的一致性(票 09c):`configs/clauses.yaml` 是真源,包内与数据包各一份**生成物**。

生成:`python tools/sync_data.py`(CI 里是打 wheel 之前的显式一步)。这条用例不替生成,
只兜住"改了真源忘了跑生成":生成链路断了,它红;跑一次生成,它绿。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "configs" / "clauses.yaml"
COPIES = (
    "src/testplatform/_data/configs/clauses.yaml",
    "packages/e2e-data/src/e2e_data/clauses.yaml",
)


def test_every_shipped_copy_of_the_clause_list_matches_the_source():
    """改真源没同步副本 → 红;改副本没同步真源 → 也红。消息点名哪一份、怎么同步。"""
    source = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    drifted = [
        relative
        for relative in COPIES
        if not (REPO / relative).is_file()
        or hashlib.sha256((REPO / relative).read_bytes()).hexdigest() != source
    ]

    assert not drifted, (
        f"条款清单内容不一致,与真源 configs/clauses.yaml 不同的是:{drifted}。"
        "重新生成:python tools/sync_data.py(真源只有 configs/clauses.yaml 一处)"
    )
