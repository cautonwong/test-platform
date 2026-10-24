# @cautonwong/e2e-data

公共数据包:周边设备型号库 / 判据集 / 标准条款清单。

**只装数据,不含用例逻辑。** 用例与场景跟着产品走,留在 monorepo。

## 现在装了什么

| 文件 | 内容 |
|---|---|
| `e2e_data/clauses.yaml` | 标准条款受控清单(条款 id → 标准 / 编号 / summary)。与平台自带的 `configs/clauses.yaml` 逐字节一致,`tests_unit/test_data_package.py` 守漂移 |

型号库与判据集目前仍在产品清单(`configs/devices*.yaml`)里,没有拆出来 —— 拆它要动清单格式
(型号按"包名 + 型号 id"引用),那是下一张票的事。

## 装

```bash
python -m build --wheel       # 在 packages/e2e-data 下
pip install dist/cautonwong_e2e_data-0.1.0-py3-none-any.whl
```

PyPI 名不能带 `@` / `/`:npm scope `@cautonwong` 派生成发行版名 `cautonwong-e2e-data`
(平台包同理:`cautonwong-testplatform`)。
