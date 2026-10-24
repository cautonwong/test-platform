"""
收尾在异常路径上的两件事 —— 只在嵌套 pytest 里被点名跑(文件名不是 test_*),因为这条用例必须真的失败:

**谁在跑它**(别把它当孤儿删掉):`tests/test_bench_restore.py::test_a_cleanup_runs_on_the_exception_path_and_does_not_mask_the_original_error`
用嵌套 pytest 按**绝对路径**点名这两个函数。目录扫描看不出这种引用(它不是 import、也不是收集期收集),
所以 tests_unit/test_orphan_case_files.py 专门守着"带 @case 却不被收集的文件必须有地方按路径点名"。

**编号**:这个文件占 `S_00_00_04_000001_01`。新增用例别再用它 —— 08 的资产目录因此把一条自己的
用例改过号;`tests_unit/test_orphan_case_files.py` 也守着这一点。

1. 用例注册的 cleanup 在用例抛异常之后照样执行,结果落进 scenario-*.json 的 cleanup 字段;
2. cleanup 自己抛异常不掩盖原异常:这条用例的结论仍是原异常(AssertionError),
   cleanup 的失败只是流程问题,记在 evidence 里。

"用例崩在中间"没有别的证明方式:它就得真崩一次,所以这组用例不进默认套件。
"""
from testplatform.cases import case

METER = "meter_01"
PARAMETER = "有功计量模式"
FACTORY_VALUE, WRITTEN_VALUE = 0, 1601


def _explode() -> None:
    raise RuntimeError("收尾自己抛异常:这段文本只能出现在 cleanup 字段里")


@case(id="S_00_00_04_000001_01", clause="DLT614-设置类条款", tags=["场景", "设置", "反例"])
def test_the_case_fails_after_declaring_cleanups(composition_root, scenario):
    """用例把参数改掉之后失败:收尾要跑,而且收尾自己的异常不许顶掉这里的原异常。"""
    meter = composition_root.get(METER)
    meter.connect()
    meter.write(PARAMETER, WRITTEN_VALUE)
    scenario.on_cleanup(lambda: meter.write(PARAMETER, FACTORY_VALUE), label="写回出厂值")
    scenario.on_cleanup(_explode, label="这条收尾自己抛异常")
    raise AssertionError("原异常:用例读到 0 kWh")


def test_the_bench_is_back_after_the_failed_case(composition_root):
    """上一条用例失败之后参数已经回到出厂值:收尾真的执行了,不是只记了一行。"""
    meter = composition_root.get(METER)
    meter.connect()
    assert meter.read(PARAMETER) == FACTORY_VALUE
