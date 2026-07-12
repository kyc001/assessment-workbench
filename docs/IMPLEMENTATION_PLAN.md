# Assessment Workbench 实施主计划

最后审计：2026-07-13  
状态：执行中  
设计基线：采用显式状态机、命题/解题分离、多维审核、结构化仲裁、分类重试、LaTeX 产物和阶段事件；所有代码、协议、数据模型和界面独立实现。 

## 使用规则

- 本文件是项目唯一的主执行清单；`docs/roadmap.md` 只保留摘要并链接到本文件。
- 只有源码、测试、文档和端到端验收全部满足“完成定义”时，任务才从 `[ ]` 改成 `[x]`。
- 只有 Schema、接口或占位实现时，不算完成。
- 每次完成任务时，同时更新“最后审计”、勾选状态和必要的实现说明。
- 工作流任务必须验证成功、失败和恢复/人工等待路径，不能只测试 happy path。
- 外部组件保持轻量可插拔：MinerU、模型服务、RAG、编译器均不得侵入领域层。

## 当前审计摘要

已实现：

- Python/uv/CLI/CI 工程基础。
- SQLite 工作区、运行记录和成对阶段事件。
- Fixture、MinerU CLI、MinerU HTTP 同步解析适配器。
- 标题层级知识图谱、可选结构化语义知识抽取和来源证据。
- OpenAI-compatible JSON Schema 模型调用与调用审计。
- 关键词检索、图谱一层扩展和知识点标签浏览。
- 基于知识点标签的 `QuestionSpec` 规划。

部分实现但尚未达到完成定义：

- `WAITING_HUMAN` 只有状态枚举，没有暂停、决策和恢复协议。
- 阶段事件已有 `round/occurrence_id/entity_id`，但缺少父事件和输入/输出产物关联。
- 材料状态机只有解析、知识抽取、持久化三阶段，尚未覆盖分类、质量审核和异常路由。
- 知识图谱存在关系 Schema，但缺少同义实体仲裁、人工修订和向量召回。
- `QuestionSpec` 已完成，但尚未生成题干、答案和评分细则。
- README 之前称“可恢复”，当前只有持久记录，尚不能从检查点继续执行。

未实现：

- Subject Profile、多科目工具和 Reviewer 配置。
- 单题生成、独立解答、Rubric、审核、仲裁与分类重试。
- 整卷蓝图、子工作流、整卷审核和人工确认。
- 统一 `ExamDocument`、LaTeX Renderer、编译和模板修复。
- 试卷 PDF 数字化。
- 答卷切题、转录、逐评分点评分、双路评分和人工复核。
- 题库/API/Web 产品层。

---

# M0 工程基础与质量门

目标：建立可重复安装、可测试、可审计的轻量 CLI 工程。

- [x] 初始化独立 Git 仓库，默认分支为 `main`。
- [x] 使用 Python 3.12、`uv` 和 `src` 布局。
- [x] 配置 Apache-2.0、README、`.env.example` 和 `.gitignore`。
- [x] 配置 Ruff、Mypy strict、Pytest 和 GitHub Actions CI。
- [x] 提供 `assessment-workbench` CLI 入口和子命令分组。
- [x] 保证默认测试离线，不依赖 MinerU、GPU、网络或付费模型。
- [ ] 增加版本发布策略、CHANGELOG 和语义版本检查。
- [ ] 增加贡献指南、开发约定和架构决策记录 ADR。

完成定义：

```bash
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
uv run assessment-workbench --help
```

全部通过，CI 执行同一组检查。

---

# M1 工作流内核、事件、恢复与人工决策

目标：把进程内状态机改造成可持久化、可恢复、支持父子工作流和人工检查点的执行内核。 

## M1.1 生命周期

- [x] 定义 `QUEUED/RUNNING/WAITING_HUMAN/SUCCEEDED/FAILED/CANCELLED/INTERRUPTED`。
- [x] 增加 `CANCELLING`，并定义取消请求和最终取消的区别。
- [x] 运行状态和当前阶段持久化到 SQLite。
- [x] 定义合法状态转换矩阵，拒绝非法跳转。
- [ ] 记录工作流输入摘要、配置快照和代码版本。

## M1.2 阶段事件

- [x] 成对记录 `running/completed/failed`。
- [x] 支持 `round` 和 `occurrence_id`。
- [x] 支持 `entity_type/entity_id` 字段。
- [x] 增加 `workflow` 字段，事件可脱离 run 单独解释。
- [x] 增加 `parent_event_id` 和 `parent_run_id`，支持题目子工作流。
- [x] 增加 `input_artifact_ids/output_artifact_ids`。
- [x] 增加结构化 `summary/warnings/error_code/error_details`。
- [ ] 数据库约束同一 `occurrence_id` 只能有一个起始事件和一个终止事件。

## M1.3 Artifact 和版本

- [x] 按 run 保存 JSON 产物。
- [x] 提供 SHA-256 计算能力。
- [x] 建立 artifact 数据表并保存 media type、大小、哈希、创建阶段和逻辑名称。
- [x] 产物写入采用临时文件加原子替换。
- [x] 同一逻辑对象使用不可变版本，不覆盖上一版。
- [x] 支持列出、读取和校验产物完整性。

## M1.4 检查点、恢复和取消

- [x] 每个成功阶段保存检查点及可序列化上下文引用。
- [x] 实现工作流引擎 `resume`，从最后一个幂等阶段继续；CLI 注册表将在后续工作流逐项接入。
- [x] 实现 `runs cancel <run-id>` 和阶段边界协作式取消。
- [x] CLI 启动时识别本机已不存在的 runner PID，将孤儿 `RUNNING/CANCELLING` 标记为 `INTERRUPTED`。
- [x] 恢复时不重复执行已完成阶段；模型调用需通过 artifact/checkpoint 输出引用接入。
- [ ] 为非幂等阶段定义补偿或禁止自动恢复。

## M1.5 人工决策

- [x] 定义 `HumanDecision`：接受、拒绝、编辑后接受、重试、终止。
- [x] 工作流可以进入 `WAITING_HUMAN` 并保存待确认对象。
- [x] 实现 `runs approve/reject/retry/abort` CLI；通过后进入可恢复状态。
- [ ] 人工编辑内容形成新版本，不覆盖 Agent 产物。
- [x] 记录操作者、时间、理由和输入/输出版本引用。

完成定义：

- 一个包含父子步骤、人工等待和故意失败的 fixture 工作流可以中断后恢复。
- 取消不会留下错误的 `RUNNING` 状态。
- 时间线能关联所有输入/输出 artifact。
- 对恢复、取消、非法状态转换和人工决策有自动测试。

---

# M2 材料导入与 MinerU 文档理解

目标：支持一学期 PDF/PPTX 等材料的增量、可审计导入，并把解析质量作为进入知识库的门禁。

## M2.1 材料类型和元数据

- [x] 支持 `LECTURE/TEXTBOOK/PAST_EXAM/PAST_SOLUTION/EXERCISE_SET/SYLLABUS/OTHER`。
- [x] 材料类型写入文档存储。
- [x] 定义 `Material` 一等对象：文件哈希、MIME、大小、课程、学期、年份、语言、用途、状态。
- [x] 自动检测文件类型和 MIME，同时允许用户指定材料用途和课程元数据。
- [ ] 支持目录批量导入和单文件失败隔离。
- [ ] 支持文件哈希去重、解析缓存和增量更新。
- [ ] 支持删除材料并标记受影响知识节点。

## M2.2 解析适配器

- [x] Fixture Parser。
- [x] MinerU CLI Parser。
- [x] MinerU HTTP 同步 `/file_parse` Parser。
- [ ] MinerU 异步 `POST /tasks` Parser 和轮询/下载协议。
- [ ] Native PDF/PPTX 轻量解析器，用于有文本层的快速路径。
- [ ] Vision fallback 端口，只在扫描件或低质量块上触发。
- [ ] Hybrid 对齐原生和视觉结果，保留冲突与置信度。
- [ ] 解析器能力协商：支持格式、公式、表格、坐标、图片和 OCR 语言。

## M2.3 统一文档模型

- [x] `ParsedDocument/ContentBlock` 基础模型。
- [x] 文本、标题、公式、表格和图片 block 类型。
- [x] 页码、标题路径和解析器元数据。
- [ ] 保存 bbox、字符/对象坐标、资源路径和视觉置信度。
- [ ] 支持 PPTX 幻灯片编号、讲者备注、隐藏页和对象层级。
- [ ] 支持题号、小问、分值和年份等考试专用块。
- [ ] 大文件采用流式写入，不把全部解析结果常驻内存。

## M2.4 材料状态机

- [ ] `MATERIAL_INIT`。
- [ ] `FILE_CLASSIFYING`。
- [x] `CONTENT_EXTRACTING`，当前名称为 `PARSING`。
- [ ] `LAYOUT_ANALYZING`。
- [ ] `FORMULA_EXTRACTING`。
- [ ] `STRUCTURE_RECOGNIZING`。
- [x] `KNOWLEDGE_EXTRACTING` 基础实现。
- [ ] `INDEX_BUILDING` 独立阶段。
- [ ] `MATERIAL_REVIEWING`。
- [ ] `MATERIAL_READY` 终态。

异常路由：

- [ ] `RETRY_EXTRACTION`。
- [ ] `USE_VISION_FALLBACK`。
- [ ] `REQUEST_DOCUMENT_TYPE`。
- [ ] `REQUEST_HUMAN_CORRECTION`。
- [ ] `REJECT_DOCUMENT`。

## M2.5 材料审核

- [ ] 结构审核：标题、页码、阅读顺序和块完整性。
- [ ] 公式审核：LaTeX 语法、截断和编号关联。
- [ ] 知识审核：内容与 Subject Profile 一致。
- [x] 来源审核基础：语义节点必须引用已有 block。
- [ ] 真题审核：识别题号、题型、分值、年份和答案对应关系。
- [ ] 审核失败阻止材料进入 `READY` 和命题检索。

完成定义：

- 可批量导入包含 PDF 和 PPTX 的课程目录。
- 重复文件不重复解析，失败文件可单独重试。
- 解析结果保留页码、公式、表格、图片和必要坐标。
- 材料审核报告可解释，并决定是否允许进入知识索引。

---

# M3 课程知识网络与轻量 RAG

目标：建立教学领域知识图谱，而不是直接依赖通用实体图谱。

## M3.1 知识模型

- [x] 标题层级 `KnowledgePoint`。
- [x] 节点 Schema：module、knowledge point、concept、definition、theorem、law、formula、experiment、problem pattern。
- [x] 关系 Schema：contains、prerequisite、derives、depends、applies、assessed、related。
- [x] 每个节点和关系包含来源 block 与置信度。
- [ ] 增加 `LearningObjective/PastQuestion/Example` 节点。
- [ ] 增加公式结构字段、符号表和适用条件。
- [ ] 增加课程、章节、学期和材料版本归属。

## M3.2 抽取和规范化

- [x] 标题层级确定性抽取。
- [x] OpenAI-compatible JSON Schema 语义抽取。
- [x] 无有效来源的语义节点和关系不入库。
- [x] 同 slug 跨材料合并证据和标签。
- [ ] 同义词、别名和中英文实体规范化。
- [ ] 冲突关系检测及仲裁。
- [ ] 环检测：先修关系不可形成非法环。
- [ ] 删除材料后的局部图谱重建。
- [ ] 图谱质量报告和人工修订导入/导出。

## M3.3 检索

- [x] 关键词检索。
- [x] 直接命中加权和匹配理由。
- [x] 图谱邻居扩展。
- [ ] 轻量本地 embedding 适配器。
- [ ] 关键词、向量和图谱融合排序。
- [ ] rerank 可选适配器。
- [ ] 按材料类型加权：课件范围、教材定义、真题风格、答案评分。
- [ ] 检索结果 token 预算、去重和来源多样性控制。

## M3.4 可选第三方 RAG

- [ ] 定义 LightRAG Backend 契约测试。
- [ ] 定义 RAG-Anything Backend 契约测试。
- [ ] 用固定课程测试集比较本地后端和第三方后端。
- [ ] 只有效果显著提升时才设为推荐，不成为核心必需依赖。

完成定义：

- 用户可以列出、搜索、展开和人工修订课程知识点。
- 每个检索结果都能追溯到原材料页码/幻灯片。
- 固定评测集包含知识点召回率、来源准确率和跨章节关系准确率。

---

# M4 Subject Profile 与多科目能力

目标：审核器、工具和题型由科目配置驱动，主状态机不硬编码物理。

- [x] 定义 `SubjectProfile` Pydantic Schema。
- [x] 支持题型、Reviewer、工具、LaTeX 模板和难度维度配置。
- [ ] 提供 `physics.yaml`。
- [x] 提供首个 `gaokao-mathematics.yaml` 数学 Profile。
- [ ] 加载时校验 Reviewer 和工具是否已注册。
- [ ] 定义 `SubjectTool` 协议。
- [ ] 接入 SymPy 符号验证。
- [ ] 实现物理量纲/单位检查器。
- [ ] 实现数值随机采样和边界情况检查器。
- [ ] Profile 版本写入所有生成和审核产物。

完成定义：

- 新增一个科目只需增加 Profile、Prompt 和可选工具，不修改工作流路由。
- 数学和物理至少各有一条通过测试的 QuestionSpec 和审核配置。

---

# M5 单题生成子工作流

目标：在 `QuestionSpec` 基础上实现命题、独立解答、Rubric、多维审核和结构化仲裁。

## M5.1 领域对象和版本

- [x] `QuestionSpec` 基础模型和来源上下文。
- [x] `QuestionVersion` 与父版本、来源和生成元数据。
- [x] `Solution/SolutionStep/SolutionVersion`。
- [x] `Rubric/RubricItem/RubricVersion`。
- [ ] `ReviewReport/ReviewFinding`。
- [ ] `ArbitrationDecision/ArbitrationFeedback`。
- [x] 每个版本保存 parent version、生成角色、模型、Prompt 版本和来源引用。

## M5.2 状态机

- [ ] `QUESTION_INIT`。
- [ ] `CONTEXT_RETRIEVING`。
- [x] `QUESTION_PLANNING` 的前置 `QuestionSpec` 工作流。
- [ ] `PROBLEM_GENERATING`。
- [ ] `SOLUTION_GENERATING`，使用独立 Agent。
- [ ] `RUBRIC_GENERATING`。
- [ ] `MULTI_REVIEWING`。
- [ ] `ARBITRATING`。
- [ ] `QUESTION_READY`。

## M5.3 Rubric

- [x] Rubric 分项之和必须等于题目分值。
- [x] 支持依赖评分点、部分分、等价表达和替代解法策略。
- [x] 支持 error carry-forward 策略字段。
- [ ] 评分点分别审核正确性、独立性、分值合理性和重复计分。

## M5.4 Reviewer Pool

- [ ] Mathematical Reviewer。
- [ ] Subject Reviewer。
- [ ] Solvability Reviewer。
- [ ] Rubric Reviewer。
- [ ] Pedagogical Reviewer。
- [ ] Structure Reviewer，确定性实现。
- [ ] Similarity Reviewer，比较往年题和同卷题。
- [ ] 独立 Reviewer 并行执行并分别记录模型调用。

## M5.5 仲裁和重试

- [ ] 决策：`PASS`。
- [ ] 决策：`PASS_WITH_WARNINGS`。
- [ ] 决策：`RETRY_PROBLEM`。
- [ ] 决策：`RETRY_SOLUTION`。
- [ ] 决策：`RETRY_RUBRIC`。
- [ ] 决策：`RETRY_PROBLEM_AND_SOLUTION`。
- [ ] 决策：`RETRY_ALL`。
- [ ] 决策：`ESCALATE_HUMAN`。
- [ ] 决策：`ABORT`。
- [ ] Pydantic/JSON Schema 验证裁决与错误严重度组合。
- [ ] 分类路由正确失效下游版本。
- [ ] 题干、答案、Rubric 和总重试预算。
- [ ] 超过预算自动进入人工处理，不无限循环。

完成定义：

- 至少能从数学和物理 fixture 知识点生成一道题、独立答案和 Rubric。
- 故意构造的题干、答案和 Rubric 错误分别触发正确重试路由。
- 所有版本和审核均可追踪，不覆盖历史。

---

# M6 整卷蓝图与 ExamGenerationWorkflow

目标：从单题扩展到整卷，支持人工蓝图确认、题目子工作流和整卷级审核。

## M6.1 ExamBlueprint

- [x] 定义 `ExamBlueprint`、Section、Coverage、DifficultyDistribution。
- [x] 校验总分、各区分值、覆盖分值和题量。
- [ ] 支持目标年级、考试时长、语言和材料范围。
- [ ] 支持用户 YAML 规格和 Agent 规划两种来源。

## M6.2 蓝图状态机

- [ ] `EXAM_INIT`。
- [ ] `INPUT_VALIDATING`。
- [ ] `MATERIALS_LOADING`。
- [ ] `EXAM_PLANNING`。
- [ ] `BLUEPRINT_REVIEWING`。
- [ ] `BLUEPRINT_ARBITRATING`。
- [ ] `WAITING_BLUEPRINT_APPROVAL`。

蓝图审核：

- [ ] 覆盖审核。
- [ ] 分值审核。
- [ ] 难度审核。
- [ ] 题型审核。
- [ ] 预计作答时间审核。
- [ ] 材料范围审核。
- [ ] 往年风格审核。
- [ ] 泄题/近似改写审核。

蓝图仲裁：

- [ ] `PASS`。
- [ ] `RETRY_COVERAGE`。
- [ ] `RETRY_DIFFICULTY`。
- [ ] `RETRY_STRUCTURE`。
- [ ] `REQUEST_USER_INPUT`。
- [ ] `ABORT`。

## M6.3 题目子工作流和组卷

- [ ] 每道题使用独立子 run 和 parent run。
- [ ] 有界并发，不超过模型和预算配置。
- [ ] 单题失败不重做整卷。
- [ ] `QUESTIONS_GENERATING/SOLVING/RUBRICS_BUILDING`。
- [ ] `QUESTIONS_REVIEWING/ARBITRATING`。
- [x] `EXAM_ASSEMBLING` 首个离线高考数学 Demo 实现。

## M6.4 整卷审核和仲裁

- [ ] 覆盖度审核。
- [ ] 难度分布审核。
- [ ] 题目重复审核。
- [ ] 分值和时长审核。
- [ ] 符号一致性审核。
- [ ] 题间依赖和答案泄露审核。
- [ ] 来源与版权风险审核。
- [ ] 排版结构审核。
- [ ] 仲裁返回具体 `question_ids/section_ids`。
- [ ] 支持 `REPLACE_QUESTIONS/REBALANCE_DIFFICULTY/REBALANCE_COVERAGE/REGENERATE_SECTION/ESCALATE_HUMAN`。
- [ ] `WAITING_EXAM_APPROVAL`。

完成定义：

- 从课程知识点和 YAML 蓝图生成 3 至 5 道题的试卷。
- 总分、覆盖和难度满足蓝图。
- 单题失败只替换对应题目。
- 人工可接受、编辑或要求重做蓝图和整卷。

---

# M7 ExamDocument、LaTeX 与 PDF

目标：内部模型不绑定特定模板宏，支持多种 Renderer，并把排版重试与内容生成解耦。 

## M7.1 文档抽象

- [ ] 定义 `ExamDocument/ExamSection/ExamQuestion` AST。
- [ ] 题干、答案、Rubric、图片、表格和公式使用结构化 block。
- [ ] JSON 是事实来源，Renderer 不反向修改领域对象。

## M7.2 Renderer

- [ ] Generic LaTeX Renderer。
- [ ] Markdown Renderer。
- [ ] JSON Renderer。
- [ ] 领域定制 Renderer 作为独立可选模板。 
- [ ] 支持题目版、答案版和评分标准版。
- [ ] 模板版本写入产物元数据。

## M7.3 校验、编译和修复

- [ ] LaTeX 结构和资源确定性检查。
- [ ] 本地 Tectonic Compiler 适配器。
- [ ] 远程异步 Compiler 适配器，参考 Latex Server 协议但独立实现。
- [ ] 编译日志解析和错误分类。
- [ ] `LATEX_FORMATTING/FORMAT_CHECKING/PDF_COMPILING`。
- [ ] `TEMPLATE_FIXING` 只修排版，不重新命题。
- [ ] PDF 页面渲染和基础视觉回归。

完成定义：

- 同一 `ExamDocument` 可稳定导出 JSON、Markdown、LaTeX 和 PDF。
- 编译失败只重试排版阶段。
- 题目版、答案版和评分标准版内容一致且总分正确。

---

# M8 题库与版本管理

目标：参考 Question_DB 的领域设计，提供轻量本地题库，再视需要服务化。

- [ ] Material、Exam、ExamSection、ExamQuestion 数据表。
- [ ] Question/Solution/Rubric 不可变版本表。
- [ ] Review、Arbitration、HumanDecision 和 Artifact 关联。
- [ ] 标签、难度、命题人、审题人和状态。
- [ ] 软删除、恢复和审计日志。
- [ ] Bundle 导入/导出。
- [ ] 题目和试卷相似度索引。
- [ ] 可选 REST API，不与 CLI 核心耦合。

完成定义：

- 任意最终试卷都能追踪到具体题目、答案、Rubric、审核和来源版本。
- 删除和恢复不会破坏引用完整性。

---

# M9 ExamDigitizationWorkflow：试卷转可编辑 LaTeX

目标：把现有试卷 PDF 转为可编辑结构和 LaTeX，并可复用解题/Rubric 工作流生成答案。

- [ ] `DIGITIZE_INIT`。
- [ ] `PDF_CLASSIFYING`。
- [ ] `CONTENT_EXTRACTING`。
- [ ] `QUESTION_DETECTING`。
- [ ] `FORMULA_RECOGNIZING`。
- [ ] `ASSET_EXTRACTING`。
- [ ] `EXAM_STRUCTURING`。
- [ ] `STRUCTURE_REVIEWING`。
- [ ] `LATEX_FORMATTING`。
- [ ] `FORMAT_CHECKING`。
- [ ] `PDF_COMPILING`。
- [ ] `RENDER_COMPARING`。
- [ ] `TEMPLATE_FIXING`。
- [ ] `WAITING_HUMAN_REVIEW`。
- [ ] 识别题号、小问、分值、题型和答案区。
- [ ] 原 PDF 坐标映射到 `ExamDocument` block。
- [ ] 可选调用 Solver/Rubric 子工作流生成草稿答案。
- [ ] 内容准确优先，不以像素级复刻为首期目标。

完成定义：

- 电子生成 PDF 可转成结构化试卷、可编辑 LaTeX 和可编译 PDF。
- 公式、图片、题号和小问正确对应。
- 人工修订后形成新版本，并可选择生成答案和 Rubric。

---

# M10 GradingWorkflow：辅助阅卷

目标：参考 AI_Scoring 的 recognize/judge/direct，但加入标准答案门禁、证据、双路评分和人工复核。

## M10.1 阅卷领域对象

- [ ] `Submission/SubmissionQuestion`。
- [ ] `Transcription`，含页码、bbox、文本、公式和置信度。
- [ ] `RubricEvaluation`。
- [ ] `GradeSuggestion/GradeReview/FinalGrade`。
- [ ] 标准答案状态：`DRAFT_STANDARD/APPROVED_STANDARD/RETIRED`。

## M10.2 三种模式

- [ ] `recognize`：只转录。
- [ ] `judge`：基于转录和 Rubric 评分，作为默认。
- [ ] `direct`：VLM 直接看答卷，作为复核或降级。

## M10.3 状态机

- [ ] `GRADING_INIT`。
- [ ] `EXAM_LOADING`。
- [ ] `ANSWER_CHECKING`。
- [ ] `STANDARD_ANSWER_PREPARING`。
- [ ] `SUBMISSION_EXTRACTING`。
- [ ] `ANSWER_SEGMENTING`。
- [ ] `ANSWER_TRANSCRIBING`。
- [ ] `RUBRIC_GRADING`。
- [ ] `SECOND_GRADING`。
- [ ] `GRADE_COMPARING`。
- [ ] `SCORE_VALIDATING`。
- [ ] `CONFIDENCE_ROUTING`。
- [ ] `WAITING_HUMAN_REVIEW`。
- [ ] `FINALIZING`。

## M10.4 无答案分支

- [ ] 调用独立 Solution Generator 两次。
- [ ] 比较候选解答并执行多维审核。
- [ ] 生成 Rubric。
- [ ] 停在 `WAITING_ANSWER_APPROVAL`。
- [ ] 未批准的 `DRAFT_STANDARD` 禁止正式批量评分。

## M10.5 评分与证据

- [ ] 每个 RubricItem 独立判定和给分。
- [ ] 每项必须引用答卷页码、bbox 和转录文本。
- [ ] 支持部分分、等价表达、替代解法和错误延续。
- [ ] 分项之和和总分确定性校验。
- [ ] 低 OCR 置信度触发 `RETRANSCRIBE` 或人工复核。

## M10.6 双路评分和仲裁

- [ ] Rubric Grader。
- [ ] Evidence Verifier。
- [ ] 低置信或高分题触发 Independent Grader。
- [ ] 比较总分和评分点冲突。
- [ ] 路由：`ACCEPT_SUGGESTION/SECOND_GRADE/RETRANSCRIBE/POSSIBLE_ALTERNATIVE_SOLUTION/POSSIBLE_STANDARD_ERROR/HUMAN_REVIEW/UNREADABLE`。

完成定义：

- 已批准标准答案下，电子 PDF 答卷能完成切题、转录、逐项建议分和证据报告。
- 无答案时必须停在人工批准，不能直接评分。
- 低置信和评分分歧能稳定进入人工复核。
- 最终导出 JSON、CSV 和 Markdown 审计报告。

---

# M11 CLI 产品面

目标：第一版命令与四条工作流一致，参数命名统一，并支持非交互自动化。

## Workspace 和运行

- [x] `workspace init`。
- [x] `runs list/inspect`。
- [ ] `runs resume/cancel/approve/reject/retry`。

## 材料和知识

- [x] `materials ingest` 单文件。
- [ ] `materials ingest` 目录批量。
- [x] `topics list/show`。
- [x] `knowledge search`。
- [ ] `knowledge export/import/edit/graph`。

## 出题和整卷

- [x] `questions plan`。
- [ ] `questions generate`。
- [ ] `exams plan`。
- [ ] `exams generate`。
- [ ] `exams export`。

## 数字化和阅卷

- [ ] `exams digitize`。
- [ ] `grade recognize`。
- [ ] `grade judge`。
- [ ] `grade direct`。
- [ ] `grade review`。

## 通用 CLI 要求

- [ ] 所有命令支持 `--json` 机器可读输出。
- [ ] 所有昂贵命令支持 `--dry-run` 或成本预估。
- [ ] 支持非交互审批文件，便于批处理。
- [ ] 错误码稳定并有操作建议。

---

# M12 评测、可观测性与安全

目标：避免系统只有演示效果，没有可量化质量。

## 评测

- [ ] 材料解析 fixture 和质量指标。
- [ ] 知识点召回率、来源准确率和关系准确率。
- [ ] 题目可解率、答案正确率、Rubric 完整率和超纲率。
- [ ] 与往年题重复率和同卷重复率。
- [ ] LaTeX 编译成功率和人工修改量。
- [ ] 阅卷与人工评分 MAE、一致率和低置信召回率。
- [ ] 每个合格题和每份答卷的 token、时间和成本。

## 可观测性

- [x] 模型角色、模型、Prompt 版本、hash、token 和错误审计。
- [ ] 结构化日志和 run correlation ID。
- [ ] 阶段耗时和模型成本汇总。
- [ ] Prompt 和模型配置快照。
- [ ] 可选 OpenTelemetry/Langfuse 适配器，不成为核心依赖。

## 安全和隐私

- [ ] API Key 不进入 artifact、日志或模型调用 payload。
- [ ] 上传文件大小、数量、路径和压缩包资源限制。
- [ ] PDF/LaTeX 外部进程超时、资源限制和隔离。
- [ ] 学生答卷数据保留和删除策略。
- [ ] 敏感信息脱敏和本地模型模式。
- [ ] 第三方组件许可证和 NOTICE 清单。

---

# M13 Web、API 与题库服务化（后置）

目标：核心 CLI 稳定后再服务化，并提供独立设计的时间线和结果查看方式。 

- [ ] FastAPI 服务层。
- [ ] OpenAPI 生成客户端，不手工复制 DTO。
- [ ] 持久任务队列和多 worker 安全执行。
- [ ] SSE/WebSocket 阶段事件。
- [ ] 用户、Token、角色和审计。
- [ ] 材料、知识图谱、题目、试卷和答卷 API。
- [ ] 工作流时间线、轮次和人工检查点 UI。
- [ ] PDF/LaTeX/Rubric/审计报告查看器。
- [ ] 阅卷三栏人工复核界面。
- [ ] PostgreSQL 和对象存储生产适配器。

完成定义：

- Web/API 只调用同一领域服务和工作流内核，不复制业务逻辑。
- 多 worker 下任务、事件和取消行为一致。

---

# 里程碑顺序与依赖

必须按以下顺序推进：

1. `M1` 工作流恢复、Artifact 和人工决策。
2. `M2` 完整材料状态机与审核。
3. `M3` 知识图谱规范化、轻量混合检索与评测。
4. `M4` Subject Profile。
5. `M5` 单题生成闭环。
6. `M6` 整卷蓝图和组卷。
7. `M7` LaTeX/PDF。
8. `M8` 题库与版本管理。
9. `M9` 试卷数字化。
10. `M10` 辅助阅卷。
11. `M11/M12` 在各里程碑同步完善。
12. `M13` 最后服务化。

依赖关系：

```text
M0
 -> M1
 -> M2
 -> M3
 -> M4
 -> M5
 -> M6
 -> M7
 -> M8
 -> M9
 -> M10
 -> M13

M11 CLI 和 M12 评测/安全贯穿 M1-M10
```

---

# 下一执行批次

当前下一批必须先补齐 M1，而不是直接继续堆 Agent：

- [x] M1-A：扩展 PhaseEvent 的 workflow、parent run/event、input/output artifact 字段。
- [x] M1-B：建立 Artifact 表、原子写入和完整性校验。
- [x] M1-C：实现状态转换矩阵和 `CANCELLING`。
- [x] M1-D：实现检查点和引擎级 resume；CLI 工作流注册随各业务流接入。
- [x] M1-E：实现 `HumanDecision` 与 `runs approve/reject/retry/abort`。
- [x] M1-F：实现 runner host/PID 所有权和进程启动时的 orphan run 恢复策略。
- [x] M1-G：补齐失败、取消、恢复、父子 run、artifact 和人工等待综合测试。

完成这一批后，才能把材料工作流扩展为完整的分类、解析、审核和异常路由状态机。
