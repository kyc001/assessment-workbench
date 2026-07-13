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
- request/response SHA-256
- token usage
- start/end time
- failure detail

知识抽取只有引用现有 `source_block_ids` 的节点和关系才会物化到课程图谱。

## Prompt 与科目能力注册表

考试生成链不再在工作流中维护 Prompt 文本或 Reviewer 名称集合：

```text
PromptRegistry
  -> PromptBundle(key, role, version, system_prompt)

CapabilityCatalog
  -> ReviewerRegistry
  -> ToolRegistry
  -> ValidatorRegistry
  -> SubjectCapabilityRegistry
```

工作流从同一个 `PromptBundle` 取得模型角色、系统 Prompt 和版本号，模型调用审计与 `GenerationMetadata` 因而不会和真实 Prompt 版本漂移。科目 Profile 在进入命题前必须通过 Reviewer、Tool 和 Validator 引用校验。

明确的 `高考数学` 请求会解析到内置能力包，锁定 19 题、150 分、120 分钟及 8 道单选、3 道多选、3 道填空、5 道解答题的结构。能力包只保存结构和行为规则，不保存题目、答案或 Rubric。未命中能力包的科目仍由 Subject Research Agent 和 Blueprint Agent 动态生成结构，再通过相同 Registry 校验。

解析优先级为：显式 Profile/Blueprint > 内置 Subject Capability > Agent 动态研究。能力包 ID/版本写入规划 Artifact，Prompt 版本写入每次模型调用和题目版本元数据。

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

每个 Reviewer 使用独立 `question_review` grandchild run。Reviewer 同批并行执行，报告完成后立即写自己的 Artifact，并更新问题级 `review-runs.json` 实时投影和不可变快照。Manifest 绑定 Question、Solution、Rubric 的具体 version ID；只有输入版本完全一致的成功报告可以复用，单个 Reviewer 失败只重试该 Reviewer。

## 轻量检索

当前 `LocalKnowledgeBackend` 提供：

- 名称、slug、描述和标签关键词匹配
- 直接命中加权
- 基于图关系的一层或多层扩展
- 跨材料证据增量合并

它不是最终的语义检索实现，但足以保持 CLI 原型轻量，并为后续向量或 LightRAG 适配器提供基准。

## 下一条垂直切片

```text
知识点标签
  -> 证据检索
  -> QuestionSpec
  -> Question Writer
  -> Independent Solver
  -> Rubric Builder
  -> Reviewer Pool
  -> Arbiter
  -> 版本化产物
```
