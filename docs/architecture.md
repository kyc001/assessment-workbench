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
