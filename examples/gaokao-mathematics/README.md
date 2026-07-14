# 高考数学整卷 Demo

这是一个由 Assessment Workbench 完成的真实整卷验收案例，展示从结构化题目 Bundle 到试题卷、答案卷和评分细则三份 PDF 的发布链路。

## 可下载产物

| 视图 | 页数 | 机器检查 | 文件 |
| --- | ---: | --- | --- |
| 试题卷 | 5 | passed，0 blocking findings | [exam-questions.pdf](artifacts/exam-questions.pdf) |
| 答案卷 | 16 | passed，0 blocking findings | [exam-solutions.pdf](artifacts/exam-solutions.pdf) |
| 评分细则 | 13 | passed，0 blocking findings | [exam-rubric.pdf](artifacts/exam-rubric.pdf) |

对应的全页渲染检查报告位于 [`artifacts/`](artifacts/)；[`run-manifest.json`](run-manifest.json) 记录来源 run、文件哈希、构建耗时和证据边界。

## 案例范围

- 科目：高考数学
- 结构：19 题，150 分，120 分钟
- 发布形式：试题、逐题解答、逐点 Rubric
- 文档流水线：LaTeX -> Tectonic -> PDF -> Poppler 全页渲染 -> 页面检查
- 发布来源：动态生成后的版本化人工修订稿，通过统一的 edited assembly 与三视图发布流程生成

## 复现新运行

```powershell
uv sync
uv run assessment-workbench workspace init workspaces/gaokao-math-demo

uv run assessment-workbench exams generate `
  --subject "高考数学" `
  --target-level "高中毕业年级" `
  --requirements "19 题，150 分，标准模拟卷" `
  --workspace workspaces/gaokao-math-demo
```

默认启用人工门禁。检查题目与整卷产物后继续：

```powershell
uv run assessment-workbench runs approve <run-id> --workspace workspaces/gaokao-math-demo
uv run assessment-workbench runs resume <run-id> --workspace workspaces/gaokao-math-demo
```

## 证据边界

这个 Demo 能证明：19 道题可以经过版本化组卷，三类 PDF 能并行编译、逐页渲染、机器检查并形成可追溯发布 Bundle。

它不能单独证明：题目达到真实高考命题质量、答案经独立专家审核，或多 Agent 在同预算下优于单 Agent。此类结论需要单独的盲评和对照实验。
