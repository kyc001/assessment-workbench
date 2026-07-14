# 架构

## 原则

1. 领域层不依赖 MinerU、模型供应商或 RAG 产品。
2. 状态机控制流程，Agent 只实现需要推理的阶段。
3. JSON 领域对象是事实来源，Markdown、LaTeX 和 PDF 是产物。
4. 所有知识和评分结论保留来源证据。
5. 外部服务失败不能损坏已完成阶段。
6. 测试默认离线运行。

## 当前垂直切片

```text
CLI
  -> MaterialIngestionWorkflow
      -> DocumentParser
      -> heading graph extraction
      -> optional semantic extraction
      -> LocalKnowledgeBackend
      -> ArtifactStore
  -> ExamAgentWorkflow
      -> capability resolution
      -> dynamic subject research pool or locked subject capability
      -> SubjectProfile / ExamBlueprint approval
      -> parallel per-question child runs
      -> whole-exam review and local arbitration
      -> three-view document build and release bundle
  -> SQLite RunStore / PhaseEvent
```

## 端口和适配器

`DocumentParser` 当前实现：

- `FixtureParser`
- `MinerUCliParser`
- `MinerUApiParser`

`KnowledgeBackend` 当前实现：

- `LocalKnowledgeBackend`，使用 SQLite

后续可选适配器：

- LightRAG
- RAG-Anything
- Docling
- Qdrant/LanceDB

这些适配器不能成为领域对象的唯一存储格式。

## 结构化模型调用

`OpenAICompatibleModel` 通过 `/chat/completions` 和 JSON Schema 请求结构化结果。模型调用审计保存：

- role
- model
- prompt version
- ContextPack Artifact ID/SHA-256
- system Prompt 与实际 strict response Schema SHA-256
- 初始/修复请求 SHA-256 序列和 repair count
- response SHA-256、provider request ID 和 finish reason
- token usage
- start/end time
- failure detail

长结构化响应使用 SSE 流式接收，adapter 在本地重建与非流式接口相同的
`choices/message/usage` 响应形状。这样首个 token 可以持续刷新上游代理的空闲超时，同时
`StructuredModel`、Schema 修复和 ModelCall 审计不需要感知传输方式。非 SSE 兼容响应仍按完整
JSON 读取。

逐题、Reviewer、Arbiter 和整卷规划调用在 HTTP 请求前写 `model-context.json`。ContextPack 保存精确结构化 user payload，并绑定当时输入 Artifact 的 run ID、artifact ID、logical name、version 和 SHA-256。它不包含 API Key、Authorization Header 或环境变量；课程上下文仍可能敏感，因此 ContextPack 只属于本地 workspace，不提交到 Git。

模型按任务风险分层，而不是全链路无差别使用同一档模型。默认 `AW_LLM_MODEL=gpt-5.6-luna`
负责题干和 Rubric 等高并发生成；默认 `AW_LLM_STRONG_MODEL=gpt-5.6-terra` 负责未知科目研究与
综合、整卷规划、独立解题、Reviewer 和 Arbiter。模型升级只用于需要更强推理的阶段；并发隔离、
恢复、Artifact 提交、Schema 校验、确定性约束和 PDF 门禁仍由运行框架负责，不能用更强模型掩盖
框架缺陷。

知识抽取只有引用现有 `source_block_ids` 的节点和关系才会物化到课程图谱。

## Prompt 与科目能力注册表

考试生成链不再在工作流中维护 Prompt 文本或 Reviewer 名称集合：

```text
PromptRegistry
  -> PromptBundle(key, role, version, system_prompt)

CapabilityCatalog
  -> ReviewerRegistry
  -> SubjectResearchRegistry
  -> ToolRegistry
  -> ValidatorRegistry
  -> SubjectCapabilityRegistry
```

工作流从同一个 `PromptBundle` 取得模型角色、系统 Prompt 和版本号，模型调用审计与 `GenerationMetadata` 因而不会和真实 Prompt 版本漂移。科目 Profile 在进入命题前必须通过 Reviewer、Tool 和 Validator 引用校验。

明确的 `高考数学` 请求会解析到内置能力包，锁定 19 题、150 分、120 分钟及 8 道单选、3 道多选、3 道填空、5 道解答题的结构。能力包只保存结构和行为规则，不保存题目、答案或 Rubric。未命中能力包的科目仍由 Subject Research Agent 和 Blueprint Agent 动态生成结构，再通过相同 Registry 校验。

解析优先级为：显式 Profile/Blueprint > 内置 Subject Capability > Agent 动态研究。能力包 ID/版本写入规划 Artifact，Prompt 版本写入每次模型调用和题目版本元数据。

## 未知科目动态研究与施测政策

未注册科目不会回退到静态题库或相邻科目模板。父运行创建三个职责独立的
`subject_research` child：课程范围、施测设计和质量政策。每个 child 使用自己的 Prompt、run、
checkpoint 和 `subject-research-report.json`，创建与完成时都立即刷新父级
`subject-research-runs.json`。一个角色失败不会取消其他角色；恢复时按输入签名复用成功报告，
只重跑缺失或失败的角色。达到 quorum 后，综合 Agent 生成可追溯的
`SubjectResearchSynthesis`，每个 Profile/Blueprint 字段通过 `ResearchFieldTrace` 引用采用的 claim
和 evidence。

综合结果必须先结构化为 `SubjectProfile` 和 `ExamBlueprint`，不能把研究自由文本直接传给
Writer。Blueprint 显式保存 `calculator_policy` 和 `difficulty_basis`；题型、题数、分值、时长、
coverage、难度分布和质量规则在人工确认后成为下游锁定合同。CLI 可以通过版本化操作修改标题、
计算器政策、难度统计口径或局部 QuestionPlan，但每次修改都创建新 Artifact，并使用 compare-and-
swap checkpoint override 防止覆盖并发更新。

## 单题阶段与 Reviewer 隔离

整卷父运行只负责规划、并发派发和状态投影。每道题是独立 child run，内部阶段为：

```text
QUESTION_INITIALIZING
  -> PROBLEM_GENERATING
  -> SOLUTION_GENERATING
  -> RUBRIC_GENERATING
  -> REVIEWS_GENERATING
  -> ARBITRATING
       -> 按依赖跳回 Problem / Solution / Rubric
       -> QUESTION_FINALIZING
```

Writer、Solver 和 Rubric 每个阶段完成后立即写不可变 Artifact 和 checkpoint。`WorkflowEngine` 的命名阶段跳转由 Arbiter 决策驱动，PhaseEvent `round` 记录重复进入次数；恢复时从最后一个已完成阶段继续，不重复调用上游模型。

题目 child 按 `AW_EXAM_QUESTION_CONCURRENCY` 有界并行，默认允许整份 18 题动态试卷同时排队；
共享 `AW_LLM_REQUEST_CONCURRENCY` 继续限制实际在途模型请求。child 创建、成功或失败都会立即更新
父级实时投影；成功 Bundle 同时写入 `editable/<parent-run>/questions/NN.json`，因此不需要等待最慢
题目即可查看、编辑或单题重跑。

每个 Reviewer 使用独立 `question_review` grandchild run。Reviewer 同批并行执行，报告完成后立即写自己的 Artifact，并更新问题级 `review-runs.json` 实时投影和不可变快照。Manifest 绑定 Question、Solution、Rubric 的具体 version ID；只有输入版本完全一致的成功报告可以复用，单个 Reviewer 失败只重试该 Reviewer。

## 整卷审核与局部替换

单题通过后，父运行不会直接进入导出，而是执行整卷级闭环：

```text
EXAM_ASSEMBLING
  -> EXAM_REVIEWS_GENERATING
  -> EXAM_ARBITRATING
       -> QUESTION_PLANS_REVISING
       -> QUESTIONS_GENERATING
       -> EXAM_FINALIZING
  -> EXAM_APPROVAL
```

确定性审核负责蓝图、计划和 Bundle 之间可证明的结构事实，包括题号、分区、题型、分值、覆盖分值、难度分布和预计时长。重复、符号与术语一致性、题间答案泄露、来源风险和排版结构由独立 `exam_review` grandchild 并行审核。每个报告绑定 `ExamDocument.id` 以及按题号排序的全部 Question、Solution、Rubric version ID；任一版本变化都会使旧报告失效。

Blueprint 的宏观覆盖桶与细粒度 `topic_tags` 分离。在调用 Planner 前，宿主根据固定 slot 分值对
coverage target 做确定性精确分区，并以分区标题和 topic tags 的语义匹配分选择稳定最优解。
`QuestionSlot.coverage_tag` 是父级锁定合同；Planner 只生成具体考点、能力目标、难度、预计时间和
命题约束，不能重分配宏观覆盖桶。每次 Schema 合法的 Planner 草稿先写 raw draft Artifact，再做
slot 与 coverage 校验；无效草稿、校验错误、反馈和下一 attempt 写入
`QuestionPlanningProgress`，暂态恢复从该进度继续。

整卷仲裁必须返回当前 Question UUID 或 Blueprint section ID。目标解析集中转换为稳定题号，未知或空目标在创建 child 前失败。`REPLACE_QUESTIONS` 和 `REGENERATE_SECTION` 只把命中题目的父级投影置为 queued；其他成功题继续复用原 child run 和 Bundle。旧 child 指针写入 `replacement_history`，新轮次用 `exam_round` 防止中断恢复时再次使刚完成的替换失效。

`REBALANCE_DIFFICULTY` 和 `REBALANCE_COVERAGE` 先只修订目标 QuestionPlan，再进入相同的局部题目生成路径。合并器要求返回的 slot 集合与目标完全一致，非目标计划保持不变。整卷重试预算耗尽或 Reviewer 无法完成时保留最新 Exam 和审核 Artifact，并进入人工审批。

仲裁之前有确定性审核护栏：只要任一 Reviewer 含 `error/fatal` 或 `passed=false`，就不能接受
`PASS`；反之，当全部 Reviewer `passed=true` 且没有 `error/fatal` 时，模型也不能因为 advisory
warning 触发重写、重规划或人工升级，只能得到 `PASS` 或 `PASS_WITH_WARNINGS`。这避免非阻断建议
演化成无限返工。

## 文档构建、页面门禁与发布

`ExamDocument` 和逐题 Bundle 是内容事实来源，LaTeX、PDF、日志和页面图片都是可重建产物。题目卷、答案卷和评分标准不再由一个同步导出循环处理，而是分别运行独立 `exam_document_build` child：

```text
DOCUMENTS_BUILDING
  -> questions: DOCUMENT_RENDERING -> PDF_COMPILING -> PDF_INSPECTING
  -> solutions: DOCUMENT_RENDERING -> PDF_COMPILING -> PDF_INSPECTING
  -> rubric:    DOCUMENT_RENDERING -> PDF_COMPILING -> PDF_INSPECTING
  -> DOCUMENT_APPROVAL
  -> RELEASE_BUNDLING
```

每个 child 先提交已校验 TeX，再调用 Tectonic。编译失败仍保存 TeX 和失败日志；兄弟视图继续执行。父运行的 `document-build-runs.json` 同时维护 editable 实时投影和不可变快照，输入签名一致且 Artifact 完整的成功视图在恢复时复用，只重跑失败、缺失或过期视图。

`generic-v3` Renderer 会把文本之间的短 `display_math` 降级为行内数学，避免模型把单个变量切成
居中段落；常见伪 LaTeX（`sqrt(...)`、`degrees`、`in {..}`、操作数间 `*`、`<=`/`>=`）、Unicode
数学符号、文本下标、单位幂和数学块中的自然语言连接词在安全校验后统一规范化。长推导可使用
`aligned` 显式换行。数学归一化不能改写 `\mathbb N^*` 等合法记号，Tectonic 日志中的缺字、
Fontconfig、overfull 和 underfull 仍是阻断错误，不能通过放宽门禁掩盖。

Poppler Inspector 使用 `pdfinfo`、`pdftotext` 和 `pdftoppm` 检查页数、A4 尺寸、文本层、连续题号、分区与视图专属标签，并把全部页面写成 PNG Artifact。灰度页只用于发现空白页和内容贴边风险。机器报告明确保留重叠、公式可读性、内容与推导正确性等人工检查项；启用 human gates 时，全部页面批准后才写 `document-acceptance.json`。

`exam-release-bundle.json` 是发布入口。它以 Artifact ID 和 SHA-256 绑定 Exam、全部逐题 Bundle、审核仲裁、ContextPack/ModelCall、三视图 TeX/PDF/日志、检查报告、页面图片和人工验收。发布前重新读取每个 Artifact 验证哈希；缺引用、失败视图或未完成的人工门禁都会拒绝发布。无 Compiler/Inspector 的内容模式可以完成到 `ExamDocument`，但不会生成发布 Bundle。

## Artifact 与阶段提交事务

Artifact 发布使用 SQLite `BEGIN IMMEDIATE` 串行化同 workspace 的版本分配。临时文件完成 flush/fsync 后原子替换到版本路径，随后在同一数据库临界区插入元数据并提交；insert 或 commit 失败会 rollback 并删除本次 final 文件。若进程在文件替换后直接终止，数据库不会暴露未提交行，下一次相同 logical name 会分配同一 version 并覆盖孤立文件；`reconcile` 只清理可识别的临时文件和未被数据库引用的 `.vN` 文件。

WorkflowEngine 通过 `RunStore.commit_phase` 在一个 SQLite 事务内提交 completed PhaseEvent 和对应 checkpoint，避免出现时间线显示阶段完成、恢复点却仍停留在上一阶段的状态。文件系统与 SQLite 不是同一个事务域，因此这里采用可恢复发布与补偿协议，而不是声称跨介质绝对 ACID。

模型 adapter 对超时、网络错误和 429/502/503/504/524 做有界请求重试。重试耗尽后统一抛出 provider 无关的 `RetryableWorkflowError`：当前 phase 仍记录 failed event，但 run 转为 `interrupted` 并保留上一 checkpoint，可用 `runs resume` 继续。题目 child 的暂态中断在父级汇总时继续传播为暂态错误，不能把可恢复的单题误写成父级永久失败；成功兄弟题仍即时落盘并在恢复时复用。旧版本已经把同类错误写成 `failed` 时，只能通过 `runs retry-failed` 恢复；该命令要求 checkpoint 和最后 failed event 命中白名单，并原子写入包含 actor、reason 和原错误的 `RUN_RECOVERY` 审计事件。领域校验、Schema 和代码错误仍是永久失败。

## 轻量检索

当前 `LocalKnowledgeBackend` 提供：

- 名称、slug、描述和标签关键词匹配
- 直接命中加权
- 基于图关系的一层或多层扩展
- 跨材料证据增量合并

它不是最终的语义检索实现，但足以保持 CLI 原型轻量，并为后续向量或 LightRAG 适配器提供基准。

## 下一条垂直切片

```text
轻量本地控制台
  -> 运行时间线和研究/题目实时 manifest
  -> Blueprint 与单题结构化编辑
  -> 单题重跑、局部重审和版本对比
  -> 内置 PDF 查看与人工页面验收
  -> 调用现有领域服务和工作流内核，不复制业务逻辑
```
