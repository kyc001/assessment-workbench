# assessment-workbench

面向数理课程的可审计试题生成、试卷数字化与辅助阅卷工作流。

当前版本是轻量 CLI 骨架，已经打通：

```text
课程材料 -> 结构化文档 -> 课程知识点/关系 -> 检索 -> 题目规格 -> 持久运行记录
```

设计采用显式状态机、多维审核、结构化仲裁和产物追踪，领域模型、代码和接口保持独立。 核心不依赖 Agent 框架、向量数据库或重型 RAG 服务。

## 特点

- MinerU 通过 CLI 或 HTTP 适配器接入，不侵入领域层
- Fixture 解析器让开发和测试完全离线
- SQLite 保存运行、事件、材料、知识点和关系
- 每个知识点保留文档页码和内容块证据
- 可选 OpenAI-compatible 语义知识抽取，严格使用 JSON Schema 输出
- 模型调用绑定版本化 ContextPack，记录 Prompt/Schema/请求序列哈希、token 和错误
- 版本化 Prompt Registry 与科目能力包，已注册科目可锁定结构，未知科目由 Agent 动态规划
- 单题 Writer/Solver/Rubric 阶段可恢复，Reviewer 独立并行运行并只重试失败项
- 整卷审核绑定全部题目版本，仲裁只重跑命中的题目或分区并保留替换历史
- 题目卷、答案卷和 Rubric 独立编译、全页检查并只重试失败视图
- 发布 Bundle 绑定内容、模型审计、审核仲裁、PDF、日志、页面图片和人工验收
- 轻量关键词检索和一层知识图谱扩展
- 从知识点标签生成带来源上下文的 `QuestionSpec`
- 为 LightRAG、RAG-Anything 等后端预留端口，但默认不安装
- 工作流阶段和产物可审计；检查点通过 Artifact 引用恢复，并支持人工接受、重试和终止

## 开发

```bash
uv sync
uv run assessment-workbench --help
uv run pytest
uv run ruff check .
uv run mypy src
```

## 快速体验

```bash
uv run assessment-workbench workspace init ./workspaces/demo

uv run assessment-workbench materials ingest \
  tests/fixtures/sample_course.json \
  --course demo-physics \
  --kind lecture \
  --parser fixture \
  --workspace ./workspaces/demo

uv run assessment-workbench topics list \
  --course demo-physics \
  --workspace ./workspaces/demo

uv run assessment-workbench knowledge search "高斯定律" \
  --course demo-physics \
  --workspace ./workspaces/demo

uv run assessment-workbench questions plan \
  --course demo-physics \
  --topic "电磁学.静电场.高斯定律" \
  --type calculation \
  --score 20 \
  --difficulty 7 \
  --workspace ./workspaces/demo

uv run assessment-workbench runs list --workspace ./workspaces/demo
```

整卷生成启用 human gates 时会先审批内容，再在三份 PDF 全页渲染后等待页面验收：

```bash
uv run assessment-workbench exams generate \
  --subject 高考数学 \
  --target-level 高考 \
  --requirements "19 题，150 分，标准模拟卷" \
  --workspace ./workspaces/gaokao

uv run assessment-workbench exams document-status \
  --parent-run <run-id> \
  --workspace ./workspaces/gaokao
```

人工修改 `editable/<parent-run>/questions/NN.json` 后，可创建新的组卷运行；它复用同一三视图构建与 PDF 门禁，不调用模型：

```bash
uv run assessment-workbench exams assemble-edited \
  --parent-run <parent-run-id> \
  --workspace ./workspaces/gaokao
```

为 edited assembly 增加 `--human-gates` 时，工作流会在全部页面 Artifact 生成后暂停；批准并恢复后，发布 Bundle 才会标记为 `human_verified`。

PowerShell 中可将多行命令改成单行执行。

## MinerU

MinerU 是独立的可选第三方组件。推荐部署为外部服务：

```bash
uv run assessment-workbench materials ingest lecture.pdf \
  --course university-physics \
  --kind lecture \
  --parser mineru-api \
  --workspace ./workspaces/physics
```

也支持调用本机 `mineru` 命令。详见 `docs/mineru-integration.md`。

## 语义知识抽取

默认材料导入只根据文档标题层级建立确定性图谱。配置 OpenAI-compatible 服务后，可增加概念、定义、定理、定律、公式、实验和题型抽取：

```bash
uv run assessment-workbench materials ingest lecture.pdf \
  --course university-physics \
  --kind lecture \
  --parser mineru-api \
  --semantic \
  --workspace ./workspaces/physics
```

需要配置：

```text
AW_LLM_BASE_URL=https://api.openai.com/v1
AW_LLM_API_KEY=...
AW_LLM_MODEL=gpt-5.6-luna
AW_LLM_STRONG_MODEL=gpt-5.6-luna
AW_LLM_REQUEST_CONCURRENCY=6
AW_EXAM_QUESTION_CONCURRENCY=3
```

抽取结果必须引用 MinerU 内容块 ID；没有证据的节点和关系不会入库。

## 路线图

完整计划、当前审计和逐项勾选状态：

- [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md)

1. 人工修订课程图谱
2. 按知识点标签生成单题、解答和评分细则
3. 多维审核、结构化仲裁和分类重试
4. 整卷蓝图与整卷审核
5. LaTeX/PDF 输出
6. 试卷数字化与辅助阅卷
