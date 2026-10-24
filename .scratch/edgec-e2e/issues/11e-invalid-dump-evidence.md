
## 补记(2026-10-24,三次不同用例名)

同一族已捕获三条不同用例:

```
test_an_explicit_root_that_only_ships_the_slave_script_runs_the_task      (11b,1/6)
test_a_project_directory_without_platform_resources_runs_a_task          (12a 分支,1 次)
test_a_project_directory_without_cases_says_so_instead_of_falling_back   (09g 分支,1 次)
```

**全部 `exit_code == 2`(verdict=invalid)**,全部在 `tests_platform/test_installed_layout.py`。
→ 不是"某一条用例偶发",是**这个文件的一类**(装形态 + 起真任务 + 判 invalid)。

**下次红的断言消息里必须自带**:verdict / reason / columns / `attribution.invalid_tests` / 一条 reading 原文
—— 有了它才能判断"invalid"是判据被环境吞掉,还是产物被污染。
