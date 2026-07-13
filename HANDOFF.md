# Assessment Workbench Handoff

最后更新：2026-07-13

## 目标

构建一个可审计、可恢复、支持多科目的考试生成工作台。用户给出科目、目标层级、约束和可选课程材料后，系统应通过真实模型工作流完成：

1. 动态研究科目能力并形成 `SubjectProfile`。
2. 动态规划结构化 `ExamBlueprint`。
3. 逐题命题，不从预设题库读取题目。
4. 由独立 Solver 解题。
5. 根据题目和独立解答构建 Rubric。
6. 由多个相互独立的 Reviewer 并行审核。
7. 由 Arbiter 进行结构化裁决和分类重试。
8. 只组装审核通过的版本化题目。
9. 输出题目版、答案版、评分标准版 LaTeX，并由 Tectonic 编译 PDF。

项目仓库：`D:\Study\26sp\agent\assessment-workbench`

远程仓库：`git@github.com:kyc001/assessment-workbench.git`

当前分支：`main`，已与 `origin/main` 同步。

## 重要约束

- 不得恢复已删除的静态 `questions.yaml` Demo。科目结构、题目、答案和 Rubric 必须由 Agent 动态生成。
- `examples/gaokao-mathematics/blueprint.yaml` 只能视为可选约束示例，不能作为题目来源。
- 可以阅读会话中指定的外部参考实现以理解通用工程模式，但本仓库不得出现其名称、品牌、缩写、路径、模板宏或源码引用。
- 不得读取、修改或提交未跟踪的 `data/`。当前 `git status` 只有 `?? data/`。
- `.env` 已被 `.gitignore` 忽略，包含本地模型端点和凭据。不要把 API Key 写入源码、日志、文档、提交或最终回复。
- 用户要求每完成一个独立步骤就提交并推送。当前相关步骤均已提交并推送。
- 最终正式产物应包含 LaTeX 和 PDF；JSON/YAML 仅作为内部状态、结构化配置或审计产物。

## 已完成工作

### 工程与持久化基础

- Python 3.12、uv、Typer、Pydantic、SQLite、Ruff、Mypy strict、Pytest 和 CI 已建立。
- 已实现材料元数据、MinerU Fixture/CLI/HTTP 适配器、知识点与语义知识抽取、关键词检索和图谱扩展。
- 已实现 OpenAI-compatible 严格结构化模型调用及模型调用审计。
- 已实现持久化工作流内核：
  - 父子 run/event。
  - immutable Artifact、原子写入、版本和 SHA-256 校验。
  - 状态转换矩阵和 `CANCELLING`。
  - checkpoint/resume。
  - `WAITING_HUMAN` 与人工决定。
  - runner host/PID 和孤儿运行恢复。
  - 阶段边界协作式取消。

### 静态 Demo 清理

- 删除了静态高考数学题库和 fixture-only workflow：
  - `examples/gaokao-mathematics/questions.yaml`
  - `src/assessment_workbench/demo_exam.py`
  - `tests/test_demo_exam.py`
  - CLI `exams demo-gaokao-math`
- 保留通用领域模型和蓝图示例。
- 对应提交：`7939075 refactor: remove static exam fixture`

### 动态 Agent 工作流

- 新增 `src/assessment_workbench/agents.py`。
- 实现 `ExamAgentWorkflow`：
  - `SUBJECT_RESEARCHING`
  - `EXAM_PLANNING`
  - `QUESTIONS_GENERATING`
  - `EXAM_ASSEMBLING`
  - `LATEX_FORMATTING`
- 新增模型角色路由 `ModelRouter`：
  - standard model 用于批量命题、Rubric 和普通 Reviewer。
  - strong model 用于科目研究、蓝图、独立解题和仲裁。
- 新增真实 CLI：

```bash
uv run assessment-workbench exams generate \
  --subject "高中数学" \
  --target-level "高中毕业年级" \
  --requirements "..." \
  --workspace workspaces/example
```

- 新增结构化领域对象：
  - `SubjectProfileCandidate`
  - `BlueprintDraft`
  - `QuestionDraft`
  - `SolutionDraft`
  - `RubricDraft`
  - `ReviewFinding`
  - `ReviewReport`
  - `ArbitrationDecision`
  - `ReviewerName`
  - 相关 severity、target 和 action 枚举
- 模型只生成草稿内容；UUID、版本号、父版本和对象间引用由宿主代码生成。
- Reviewer 通过 `asyncio.gather` 并行运行，彼此不读取其他 Reviewer 结果。
- `structure` Reviewer 是确定性实现，不调用模型。
- Arbiter 使用严格 Schema 输出，并有本地严重度门禁：存在 `ERROR/FATAL` finding 时不得 `PASS`。
- 分类重试已实现：
  - `RETRY_PROBLEM` 重写题干，并使下游 Solution/Rubric 失效。
  - `RETRY_SOLUTION` 保留题干，只重写 Solution 和 Rubric。
  - `RETRY_RUBRIC` 只重写 Rubric。
  - `RETRY_ALL` 全部重写。
  - `PASS`、`PASS_WITH_WARNINGS`、`ESCALATE_HUMAN`、`ABORT` 已定义。
- 测试明确验证：第一轮 Solution 错误时 Writer 仅调用一次，Solver 和 Rubric Builder 各调用两次。
- 对应提交：`bc0d17c feat: add dynamic exam agent workflow`

### LaTeX 与 PDF

- 新增 `src/assessment_workbench/latex.py`：
  - `ExamView.QUESTIONS`
  - `ExamView.SOLUTIONS`
  - `ExamView.RUBRIC`
  - `GenericLatexRenderer`
  - 普通文本 LaTeX 转义
  - 数学表达式危险命令拒绝
- 新增 `src/assessment_workbench/compilers.py`：
  - `TectonicCompiler`
  - 临时目录隔离
  - 参数数组调用，`shell=False`
  - job name 白名单
  - 编译超时
  - UTF-8 日志解码，非法字节替换
  - 日志临时路径规范化
  - PDF 文件头校验
- 工作流输出：
  - `exam-questions.tex/pdf`
  - `exam-solutions.tex/pdf`
  - `exam-rubric.tex/pdf`
  - 每个视图对应 Tectonic 日志
- 对应提交：`15a4341 feat: render and compile exam documents`

### 模型调用可靠性

- 严格 JSON Schema 会递归补齐：
  - 每个对象的全部属性进入 `required`。
  - 每个对象设置 `additionalProperties: false`。
- Reviewer 名称已改为 `ReviewerName` 枚举，避免模型生成近义但未注册的名称。
- 传输重试只覆盖可恢复错误：
  - 网络异常和 timeout。
  - HTTP `429/502/503/504/524`。
  - 最多三次请求，退避为 0.5 秒、1 秒。
  - HTTP 400 和业务校验错误不会被盲目重试。
- Pydantic 结构校验失败时，会把校验错误反馈给同一模型做一次定向 JSON 修复；第二次仍非法则明确失败。
- 对应提交：`5bf32e3 fix: harden structured model execution`

## 修改过的文件

### 当前保留并修改

- `.env.example`
  - 增加 `AW_LLM_STRONG_MODEL`、`AW_TECTONIC_COMMAND`、`AW_TECTONIC_TIMEOUT` 示例。
- `docs/IMPLEMENTATION_PLAN.md`
  - 移除静态 Demo 完成声明。
  - 更新动态 Subject Research、Blueprint、Writer/Solver/Rubric、Reviewer/Arbiter、LaTeX/Tectonic 的完成状态。
- `examples/gaokao-mathematics/blueprint.yaml`
  - ID 改为约束示例语义，不再称 Demo。
- `src/assessment_workbench/agents.py`
  - 动态考试 Agent 工作流、模型路由、分类重试、审核、仲裁、组卷和导出。
- `src/assessment_workbench/cli.py`
  - 删除静态 Demo 命令。
  - 增加 `exams generate`。
- `src/assessment_workbench/compilers.py`
  - Tectonic 编译适配器。
- `src/assessment_workbench/config.py`
  - strong model 与 Tectonic 配置。
- `src/assessment_workbench/domain.py`
  - 草稿、审核、仲裁和 Reviewer 枚举等结构化模型。
- `src/assessment_workbench/latex.py`
  - 通用 LaTeX Renderer。
- `src/assessment_workbench/models.py`
  - strict Schema 规范化、可恢复 HTTP 重试、一次结构化输出修复。
- `tests/test_agents.py`
  - 动态链路和分类重试测试。
- `tests/test_latex.py`
  - 三视图、转义、安全和真实 Tectonic 集成测试。
- `tests/test_models.py`
  - strict Schema 递归规范化测试。

### 已删除

- `examples/gaokao-mathematics/questions.yaml`
- `src/assessment_workbench/demo_exam.py`
- `tests/test_demo_exam.py`

### 本地但未跟踪/未提交

- `.env`
  - 已被 `.gitignore` 忽略。
  - 包含本地模型端点、API Key、默认模型、强模型和 Tectonic 路径。
  - 不要在文档或提交中公开其内容。
- `workspaces/`
  - 已被 `.gitignore` 忽略。
  - 包含真实端到端运行记录和生成产物。
- `data/`
  - 用户文件，未读取、未修改、未提交。

## 关键设计决定

1. **不使用静态题库作为 Demo**
   - 最终题目必须来自模型工作流。
   - YAML 只作为约束或内部结构化状态。

2. **草稿 DTO 与领域版本分离**
   - 模型不生成 UUID、版本、父引用或交叉引用。
   - 宿主将 `QuestionDraft/SolutionDraft/RubricDraft` materialize 为版本化领域对象。

3. **命题与解题分离**
   - Solver 只读取题目和上下文，不读取 Writer 的隐藏答案或 Rubric。

4. **审核独立并行**
   - Reviewer 只读取同一 bundle，不读取其他 Reviewer 报告。
   - Arbiter 是唯一汇总全部审核意见的角色。

5. **分类重试按依赖图失效**
   - 题干变化必然使 Solution 和 Rubric 失效。
   - Solution 变化必然使 Rubric 失效。
   - Rubric 错误不需要重写题干或解答。

6. **确定性门禁优先于模型裁决**
   - Pydantic 校验、对象引用、Rubric 总分和严重度门禁不能被 Arbiter 覆盖。

7. **模型路由按任务难度分层**
   - 本地 `.env` 当前配置 standard model 为中等成本模型，strong model 为较强推理模型。
   - 具体模型名称应通过环境变量配置，不硬编码到工作流。

8. **LaTeX Renderer 与 Compiler 解耦**
   - Renderer 是纯转换，不写文件、不执行外部进程。
   - Compiler 只接受生成的源字符串，在临时目录运行。
   - 编译失败不应回到命题阶段。

9. **外部实现仅作为模式参考**
   - 采用了阶段级/总量重试、审核并行、结构化仲裁和编译进程隔离等通用模式。
   - 没有复制其品牌、提示词、专用宏、路径或源码文本。

## 执行过的主要命令和结果

### 静态 Demo 移除后

```bash
uv run ruff check .
uv run mypy src
uv run pytest
```

结果：Ruff、Mypy 通过，`26 passed`。

### 动态 Agent 工作流完成后

```bash
uv run ruff check .
uv run mypy src
uv run pytest
```

结果：Ruff、Mypy 通过，`27 passed`。

### LaTeX/Tectonic 完成后

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

结果：全部通过，随后增加集成测试后达到 `30 passed`。

Tectonic 探测：

```powershell
& "C:\Users\kyc\.local\bin\tectonic.cmd" --version
```

结果：`Tectonic 0.16.9`。

真实中文编译探测成功，生成 5231 字节 PDF。日志有非致命 Fontconfig 提示：

```text
Fontconfig error: Cannot load default config file: No such file: (null)
```

### 最终质量门

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

结果：

```text
All checks passed!
30 files already formatted
Success: no issues found in 16 source files
31 passed
```

### 真实端到端成功运行

```bash
uv run assessment-workbench workspace init workspaces/e2e-6
uv run assessment-workbench exams generate \
  --subject "高中数学" \
  --target-level "高中毕业年级" \
  --requirements "生成一份仅含1道10分解答题的短测，考试时间20分钟，只考查一元二次方程，覆盖分值必须为10分，中等难度。" \
  --workspace workspaces/e2e-6
```

结果：

```text
Run: 43ff9daa-9386-4162-9812-1aebc1aeae5c
Status: succeeded
Questions: 1
Total score: 10
```

成功 run 的阶段：

```text
SUBJECT_RESEARCHING
EXAM_PLANNING
QUESTIONS_GENERATING
EXAM_ASSEMBLING
LATEX_FORMATTING
DONE
```

产物目录：

```text
workspaces/e2e-6/artifacts/43ff9daa-9386-4162-9812-1aebc1aeae5c/
```

Artifact 完整性检查：

```text
artifacts=18
verified=18
```

关键产物：

- `exam.v1.json`
- `exam-questions.v1.tex`
- `exam-questions.v1.pdf`
- `exam-solutions.v1.tex`
- `exam-solutions.v1.pdf`
- `exam-rubric.v1.tex`
- `exam-rubric.v1.pdf`
- 三份 Tectonic 日志
- Subject Profile、Blueprint、Question、Solution、Rubric、Reviews、Arbitration 中间产物

## 失败验收记录和对应修复

这些失败 run 保留在被忽略的 `workspaces/` 中，用于审计，不需要删除。

### `workspaces/e2e`

- Run：`b06c2eed-016f-46ff-9595-bedefe36166a`
- 失败：HTTP 400。
- 根因：strict Schema 中带默认值字段没有全部进入 `required`。
- 修复：`_strict_schema()` 递归补齐 `required`。

### `workspaces/e2e-2`

- Run：`7ef98256-e3a1-4c00-abbf-1ef0821e7958`
- 失败阶段：`EXAM_PLANNING`。
- 根因：strict Schema 对象缺少 `additionalProperties: false`。
- 修复：`_strict_schema()` 为所有对象递归设置 `additionalProperties: false`。

### `workspaces/e2e-3`

- Run：`ba485238-5c4f-4f6d-b0ad-6dd61e4d5251`
- 已走到 Tectonic 编译。
- 失败：Windows 子进程按 GBK 解码 UTF-8 Tectonic 日志，stdout reader 触发 `UnicodeDecodeError`，随后字符串拼接遇到 `None`。
- 修复：编译器固定 `encoding="utf-8"`、`errors="replace"`。

### `workspaces/e2e-4`

- Run：`9ff4a1fa-1c46-4e60-870b-6c4a30dc9714`
- 失败阶段：`QUESTIONS_GENERATING`。
- 失败：网关 HTTP 524。
- 修复：模型客户端对网络异常和 `429/502/503/504/524` 增加有界指数退避。

### `workspaces/e2e-5`

- Run：`1e705d60-da8f-4ad0-b2f7-18bb75678529`
- 失败：模型生成的 Rubric 某项 partial credit 等于该项满分，被本地 Pydantic 正确拒绝。
- 修复：结构化响应第一次校验失败时进行一次定向 JSON 修复调用。

## 已知错误和限制

1. **LaTeX 文本/数学 AST 尚未完成**
   - `QuestionVersion.statement` 和 `SolutionVersion.final_answer` 仍是普通字符串。
   - Renderer 会转义题干中的 `^`、`_` 等字符，因此当前题目版 PDF 中类似 `x^2` 会显示为文本形式，而不是数学公式。
   - `SolutionStep.expression` 是当前唯一按受限数学 LaTeX 渲染的字段。
   - 下一步应引入 `TextBlock | MathBlock | ImageBlock | TableBlock`，不要用正则猜测 `$...$`。

2. **最终答案字段可能包含 Markdown/LaTeX 混合内容**
   - 当前答案版 Renderer 会把 `final_answer` 当普通文本转义。
   - 真实成功 run 中 Solver 返回了较长的 Markdown + LaTeX 最终答案，PDF 可编译，但数学排版质量有限。

3. **工作流恢复仍不适用于完整动态 Agent 链**
   - `WorkflowEngine` checkpoint 只能保存标量和字符串列表。
   - `ExamAgentWorkflow` 当前把 Pydantic 对象保存在内存 state 中，没有完全采用“artifact ID 作为跨阶段事实来源”。
   - 进程中断后不能可靠从 Question/Solution/Rubric artifact 恢复。

4. **每道题尚未使用独立 child run**
   - 当前整卷逐题串行生成，所有题共享父 run。
   - 尚未实现有界并发和单题失败隔离。

5. **重试预算仍较粗**
   - 当前使用 `max_question_attempts` 总尝试上限。
   - 尚未分别维护 problem、solution、rubric 和总预算。
   - 预算耗尽当前抛出失败，尚未自动进入 `WAITING_HUMAN`。

6. **人工检查点尚未接入蓝图和整卷生成**
   - 内核支持人工审核，但动态工作流未提供蓝图批准、整卷批准、编辑后接受流程。

7. **LaTeX 阶段事件不够细**
   - 工作流阶段名为 `LATEX_FORMATTING`，但该 step 内同时完成三种渲染和 PDF 编译。
   - 尚未拆成 `FORMAT_CHECKING`、`PDF_COMPILING`、`TEMPLATE_FIXING`。

8. **编译隔离不是完整沙箱**
   - 已使用临时目录、参数数组、超时和 `shell=False`。
   - 尚未使用容器、低权限独立用户、CPU/内存/进程数限制。
   - 首次 Tectonic 编译可能联网下载 bundle；生产模式尚未加入 `--only-cached`。

9. **PDF 未做视觉回归**
   - 当前代理运行环境无法直接读取 PDF 页面进行目视检查。
   - 已验证文件存在、以 `%PDF-` 开头、Tectonic 返回成功、Artifact 哈希正确。
   - 尚未渲染为 PNG 并做页面级视觉检查。

10. **Tectonic 日志有非致命 Fontconfig 提示**

```text
Fontconfig error: Cannot load default config file: No such file: (null)
```

   - 当前不影响 PDF 生成。
   - 后续可为 Tectonic 子进程提供明确的 Fontconfig 配置或锁定 bundle/font 环境。

11. **当前生成流程没有外部 Web research adapter**
   - Subject Research 只依赖用户要求和传入的 source context。
   - 没有自动检索官方考纲或公开来源。
   - 不应把模型的通用知识表述成已验证外部来源。

12. **`source` 目前只接受预先准备的 UTF-8 文本文件**
   - CLI 尚未直接从已导入材料和知识库自动组装 source context。

13. **真实整卷尚未运行**
   - 已完成真实单题、10 分端到端验收。
   - 尚未用真实模型生成完整 150 分、约 20 题高考数学模拟卷。
   - 当前串行多 Reviewer 调用成本和时间较高，应先实现子 run、并发限制和恢复，再运行完整整卷。

## 尚未完成事项

优先级较高：

- 定义结构化内容 AST，并升级题干、解答和 Rubric 的数学表达。
- 将动态工作流各阶段改为从 ArtifactStore 读取类型化 artifact，checkpoint 只保存 artifact ID。
- 每题使用独立 child run，并支持有界并发、单题失败隔离和单题恢复。
- 增加 problem/solution/rubric/total 独立重试预算，耗尽后进入人工审核。
- 接入蓝图人工批准与整卷人工批准。
- 拆分 `LATEX_FORMATTING`、`FORMAT_CHECKING`、`PDF_COMPILING` 和 `TEMPLATE_FIXING`。
- 增加整卷级覆盖、难度、重复、时长、符号一致性和题间泄露审核。
- 自动从 workspace 材料/知识库构建 source context。
- 增加 PDF 页面渲染和视觉回归。

中后期：

- Markdown Renderer、JSON Renderer 和领域模板 Renderer。
- 远程异步 Compiler adapter。
- 题库持久化、API、Web 时间线和 PDF 预览。
- 试卷数字化与辅助阅卷工作流。

详细清单以 `docs/IMPLEMENTATION_PLAN.md` 为准。

## 下一步建议

推荐按以下顺序接手，避免直接运行昂贵的完整 150 分整卷：

1. **先补结构化内容 AST**
   - 新增 `TextBlock`、`InlineMathBlock`、`DisplayMathBlock`、`ImageBlock`、`TableBlock`。
   - 将题干和最终答案从混合字符串迁移到 block 列表。
   - Renderer 只转义 TextBlock，只允许 MathBlock 中的受限 LaTeX。

2. **改造 Artifact 驱动恢复**
   - 为 `ArtifactStore` 增加类型化 JSON 读取辅助。
   - 每个阶段返回并 checkpoint 对应 artifact ID。
   - 恢复时不重复调用已经成功的模型角色。

3. **拆出 Question 子工作流**
   - 每题创建 child run，关联 parent run。
   - 把 Writer、Solver、Rubric、Reviewer、Arbiter 拆成可恢复阶段。
   - 在子工作流内实现显式条件跳转或可靠的预展开重试阶段。

4. **完善预算和人工路由**
   - 独立统计 problem、solution、rubric 和 total retry。
   - 超预算进入 `WAITING_HUMAN`，不要直接失败。

5. **完善排版阶段**
   - 增加确定性格式检查。
   - 编译失败只进入模板修复，不重新命题。
   - 增加 `--only-cached` 和可配置 Tectonic cache。

6. **进行中等规模真实验收**
   - 先生成 3 至 5 题、30 至 50 分试卷。
   - 检查每题独立恢复、分类重试和整卷审核。
   - 成功后再运行完整高考数学 150 分试卷。

## 当前 Git 状态

交接文档创建前：

```text
## main...origin/main
?? data/
```

最近提交：

```text
5bf32e3 fix: harden structured model execution
15a4341 feat: render and compile exam documents
bc0d17c feat: add dynamic exam agent workflow
7939075 refactor: remove static exam fixture
```

创建本文件后会新增未提交的 `HANDOFF.md`；`data/` 仍必须保持不动。
