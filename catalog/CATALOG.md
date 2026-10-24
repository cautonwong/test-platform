# 资产目录(生成物,不要手改)

由清单 + 条款清单 + 测试文件扫描生成。校验: `python tools/catalog.py --verify`。

- 型号 5 · 设备 6 · 用例 79 · 条款 20 · 缺口 0

## 已知豁免(验不了,理由在此)

- `DLT645-4.5.3`: 本仓库没有该条款原文,DL/T645 型号的点表也没有可对应到它的量:TODO(来源)拿到条款原文后重判
- `GBT37408-5.2.5`: 没有条款原文,也没有对应到它的设备能力/点表量,无法构造可判定的用例:TODO(来源)

## 缺口

没有缺口。

## 型号

| 型号 | 类型 | 判据集 | 用例 | 条款 | CI 可跑 | 可注入故障 |
|---|---|---|---|---|---|---|
| battery_lfp_series | battery | battery_default | 5 | 5 | gateway_ssh✓、serial✓、tcp✓ | 无 |
| dlt645_meter | meter | meter_220v | 4 | 4 | gateway_ssh✓、serial✓ | 无 |
| dtsd1352_modbus | meter | meter_220v | 4 | 4 | gateway_ssh✓、serial✓、tcp✓ | bad-crc、link-lost、slow、timeout |
| grid_simulator_3ph | grid_simulator | — | 0 | 0 | gateway_ssh✓、serial✓、tcp✓ | 无 |
| inverter_pv_series | inverter | grid_50hz | 7 | 7 | gateway_ssh✓、serial✓、tcp✓ | 无 |

## 条款覆盖

| 条款 | 用例数 |
|---|---|
| DLT614-设置类条款 | 2 |
| DLT645-4.5.1 | 1 |
| DLT645-4.5.2 | 2 |
| DLT645-4.5.3 | 0 |
| DLT645-4.5.4 | 1 |
| DLT645-4.5.5 | 1 |
| GBT17215.321-走字误差 | 3 |
| GBT34131-5.1.1 | 1 |
| GBT34131-5.2.1 | 2 |
| GBT34131-5.2.2 | 1 |
| GBT34131-5.2.3 | 1 |
| GBT34131-5.2.4 | 1 |
| GBT37408-5.1.1 | 1 |
| GBT37408-5.1.2 | 1 |
| GBT37408-5.2.1 | 4 |
| GBT37408-5.2.2 | 1 |
| GBT37408-5.2.3 | 1 |
| GBT37408-5.2.4 | 1 |
| GBT37408-5.2.5 | 0 |
| GBT37408-5.3.1 | 1 |
