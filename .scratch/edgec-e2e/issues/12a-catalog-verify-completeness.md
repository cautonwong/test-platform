# 12a `catalog --verify` 漏比 version / counts / waived

Status: resolved
来源:clawpatch 切片审查(provider=pi),leader 已抽验成立。

## 事实(leader 读过 `catalog/verify` 现文)

```python
def verify(catalog, out_dir):
    ...
    for section in ("cases", "models", "devices", "clauses", "criteria_profiles"):
        differences.extend(_diff_section(section, written.get(section) or {}, current[section]))
    differences.extend(_diff_gaps(written.get("gaps") or [], current["gaps"]))
    return differences
```

**只比了 5 段 + gaps**;目录里这几栏变了 `--verify` 仍然 rc=0:

- `version`(目录 schema 版本)
- 各段 `counts`
- **`waived`(已知豁免,票 18 加的)**

后果:**豁免被悄悄改掉 / 删除,`--verify` 不会红** —— 而豁免正是"这条标准我们不验"的唯一记录。

## 要求

1. `verify` 补比:`version`、`counts`、`waived`(逐条:条款号 + 理由原文)
2. 用例三态:改 version / 改 count / 改豁免理由 → **各自必红**,消息指出具体字段
3. **不许**把 `waived` 并进 `gaps` 比 —— 它们是两种事实("已知不验" vs "该验没验")
4. 变异:把新加的任何一段比对删掉 → 必红

## 边界

不改 `docs/**`;不动 `catalog/catalog.json` 的内容(只重新生成一次让校验通过)。

## Comments

- 12a 已合入 4c08bfc:补比 version / counts / waived;三态用例齐,`waived` 不并进 gaps。
