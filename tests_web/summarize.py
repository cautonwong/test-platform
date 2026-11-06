"""把 tests_web 的证据汇总成一份 summary.json（平台报告口径）。

四档结论里本套用例只会产出两类：
  - product_ok:用例通过，且证据里记录了服务端实际返回的关键字段
  - product_fail:断言失败（pytest 会先红，这里只做归档）
缺证据文件 = evidence_broken，不能当成通过。
0 条证据同样不能当成通过：那说明一条用例都没跑（通常是被 skip）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

EVIDENCE_DIR = Path(sys.argv[1] if len(sys.argv) > 1 else "evidence/webui-e2e")
SUMMARY = EVIDENCE_DIR / "summary.json"


def main() -> int:
    if not EVIDENCE_DIR.exists():
        print(f"缺少证据目录 {EVIDENCE_DIR}")
        return 2

    files = sorted(p for p in EVIDENCE_DIR.glob("WUI_*.json"))
    cases = []
    broken = []
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            broken.append(path.name)
            continue
        if "caseId" not in payload or "verdict" not in payload:
            broken.append(path.name)
            continue
        cases.append(payload)

    summary = {
        "suite": "webui-e2e",
        "target": "EdgeC web/ (Core + Product Pack + 静态 Registry)",
        "clauseSource": "web/docs/边缘网关_WebUI平台详细设计文档_v1.6_最终设计基线.md",
        "total": len(cases) + len(broken),
        "product_ok": sum(1 for c in cases if c["verdict"] == "product_ok"),
        "product_fail": sum(1 for c in cases if c["verdict"] != "product_ok"),
        "evidence_broken": broken,
        "cases": [{"caseId": c["caseId"], "clause": c["clause"], "verdict": c["verdict"]} for c in cases],
        "notes": [
            "被测对象是 EdgeC 的 webd 行为替身（mock-server）：它按 Registry + OpenAPI 元数据执行授权，",
            "但 TLS/IPC/审计持久化/任务执行器未实现，不能替代真机验证。",
        ],
    }
    SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if summary["total"] == 0:
        # 0 条证据不是"全部通过":它只证明这套用例一条都没跑(通常是被 skip 了)
        print("0 条证据 = 未完成:没跑过不等于通过")
    print(f"{summary['product_ok']}/{summary['total']} product_ok, evidence_broken={len(broken)}")
    print(f"→ {SUMMARY}")
    ok = summary["total"] > 0 and summary["product_fail"] == 0 and not broken
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
