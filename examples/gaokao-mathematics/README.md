# 高考数学动态生成 Demo

该示例只预设考试结构，不包含任何静态题目、答案或 Rubric。运行时会使用锁定的 19 题、150 分、120 分钟蓝图，并通过 Agent 动态生成每道题及其独立解答、Rubric、审核和仲裁结果。

先初始化工作区：

```powershell
uv run assessment-workbench workspace init workspaces/gaokao-math-demo
```

再运行生成命令：

```powershell
uv run assessment-workbench exams generate `
  --subject "高考数学" `
  --target-level "高中毕业年级" `
  --requirements "按当前全国统一考试数学结构生成原创模拟卷，不直接改写既有真题。" `
  --workspace workspaces/gaokao-math-demo
```

`高考数学` 会自动解析到内置能力包。能力包锁定结构、命题规则、Reviewer 和确定性校验器；`examples/` 下的 Profile 与 Blueprint 仍可作为显式约束示例。使用普通 `高中数学` 或其他未注册科目时，系统不会套用整卷结构，而是由 Subject Research Agent 和 Blueprint Agent 动态设计。

该命令会触发 19 道题的完整动态生成和审核链，模型调用数量较多。自动化测试只使用小型类型化 fixture 验证预设分支，不会运行完整真实模型整卷。

CLI 默认在整卷组装后暂停，确认产物后继续：

```powershell
uv run assessment-workbench runs approve <run-id> --workspace workspaces/gaokao-math-demo
uv run assessment-workbench runs resume <run-id> --workspace workspaces/gaokao-math-demo
```

无人值守的验收运行可以在生成命令中增加 `--no-human-gates`。该选项只关闭人工暂停，不会关闭题目审核、仲裁或确定性校验。
