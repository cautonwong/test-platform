"""
生成随包走的 `clauses.yaml` 副本(票 09c)。

    python tools/sync_data.py     # 从真源重新生成两份副本(幂等:内容一样就不写)

真源只有 `configs/clauses.yaml` 一处;下面两份是**生成物**,不要手改:
    src/testplatform/_data/configs/clauses.yaml   随 wheel 走
    packages/e2e-data/src/e2e_data/clauses.yaml   公共数据包

`tests_unit/test_data_copies.py` 兜住"改了真源忘了跑生成" —— 它不替生成,只报漂移。
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / "configs" / "clauses.yaml"
COPIES = (
    "src/testplatform/_data/configs/clauses.yaml",
    "packages/e2e-data/src/e2e_data/clauses.yaml",
)


def main() -> int:
    content = SOURCE.read_bytes()
    updated = []
    for relative in COPIES:
        target = REPO / relative
        if target.is_file() and target.read_bytes() == content:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        # 字节拷贝:两份副本要与真源逐字节一致(一致性用例比的是 sha256,不做文本规范化)
        target.write_bytes(content)
        updated.append(relative)

    print(
        f"clauses 副本:{'更新 ' + str(updated) if updated else '已是最新'}"
        f"(真源 {SOURCE.relative_to(REPO).as_posix()})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
