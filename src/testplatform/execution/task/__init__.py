"""
任务执行器 —— 一份任务文件 → 一条命令跑完:分配端口、拉起模拟从站、起产品、等它就绪、
跑选中的用例、落证据、给出退出码。

任务文件的形状(票 01 定形:五个必填字段 + 一个可选的 kind):
    id       这一轮的名字,也是证据目录名的一部分
    product  只管身份与追踪,不参与任何判定
    launch   命令行。占位符是**闭集**:
               ${PORT}              平台分配的产品监听端口(必须有 —— 写死数字并行时会撞端口)
               ${SLAVE_PORT:<设备id>}  那台模拟从站的端口,<设备id> 必须在这份任务引用的清单里
               ${PYTHON}            当前解释器(任务文件不绑死某个 python 路径)
    devices  设备清单,相对**项目目录**解析(清单与用例一样只来自项目目录;launch 里的脚本路径
             多一档包内兜底,见 `_resolve_product_command`)
    select   用例筛选,与 --select 同一套写法(model: / capability: / tag: / case:)
    kind     可选,这一轮是哪种:acceptance(缺省,验收设备)/ smoke(只验通路,不验设备)
             取值是闭集:写别的在加载期报错。静默当 acceptance 会让「设备级 0/0」有两种读法,
             而冒烟任务验的本来就不是设备(见 reporting/report.py 的 0/0 分叉)。

加载期报错必须指出是哪一条:清单不存在、产品命令不可执行、占位符拼错、从站 id 不存在、
select 写法不认识、kind 不在闭集里。留到跑起来再报,现场只会看到"产品起不来" —— 而那可能是清单、命令、
端口三件事中的任何一件。

模拟从站用 `tools/mock_modbus_meter.py`(真进程、真 socket),说哪种帧由该设备型号的 protocol 推导:
`modbus_tcp` → MBAP 帧,其余(含拼错的规约名)一律 RTU 帧 —— 帧格式不写第二遍。从站地址与寄存器初值
取清单里那台设备自己声明的 `bus_address` / `connection.simulated_registers`:平台侧读同一台表、
产品也读同一台表,两边读到的东西才有可比性。

产品进程的死因按「平台自己有没有发起过终止」分类,不看退出码(票 03):
    self_exit        产品自己退出(非零码 / 收到信号)= 产品不合格;证据记下 how=self_exit 与 code / signal
    platform_killed  平台收尾、任务超时、取消、重启 —— 终止本身不产生结论,已跑完的用例保持各自结论
    起不来            产品没到就绪就没了(可执行缺失 / 参数错 / 权限)= 平台错误,不记 product_exit
被平台叫停时没跑到的用例单独记一条 unexecuted{count, reason},与产品死因分开 —— 报告要能分开陈述。

证据落在任务自己的目录里(缺省 `evidence/task-<id>-<UTC 时间戳>/`):
    task.json         这一轮的任务事实:替换后的实际命令行、分配到的端口、就绪等待时长、退出码,
                      端口来源(port_source / port_range / port_attempts)、
                      runtime(启动 / 首个读数 / 总耗时)、env(commit / 平台版本 / 产品摘要 / 模拟器版本),
                      process_events(死因记录 product_exit、重启点 product_restart、未执行 unexecuted)
    每设备 JSONL      由证据层写(任务目录就是 pytest 那一轮的 --evidence-dir)
    run-scope.json    这一轮的范围(收集 / 执行 / 跳过 / 失败)
    round-status.json 这一轮状态(有没有断链)
    product.out/.err  产品自己的 stdout / stderr
    report.md         这一轮的结论(四档 + 两栏通过率),人读;summary.json 是同一份的机器读版
"""

from __future__ import annotations

from . import config, runtime

# 调用方一直从 testplatform.execution.task 取名字;包内怎么分不改变这个入口。
for _module in (config, runtime):
    for _name, _value in vars(_module).items():
        if not _name.startswith("__"):
            globals().setdefault(_name, _value)
del _module, _name, _value
