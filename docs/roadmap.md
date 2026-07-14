# 路线图摘要

完整、可勾选的执行计划和当前实现审计见：

- [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md)

本文件只保留里程碑摘要，不作为进度判断依据。

## M0：轻量骨架

- [x] Git、uv、质量工具
- [x] 显式工作流和阶段事件
- [x] SQLite 运行与知识存储
- [x] Fixture/MinerU CLI/MinerU API 解析器
- [x] 来源可追踪的标题知识图谱
- [x] topics/runs CLI

## M1：课程知识抽取

- [x] OpenAI-compatible 结构化模型端口和调用审计
- [x] 概念、定义、定理、定律、公式、实验和题型抽取 Schema
- [x] 先修、推导、应用和考查关系 Schema
- [x] 基于稳定 slug 的增量合并
- [x] 关键词与图谱扩展检索
- [ ] 同义实体规范化与冲突仲裁
- [ ] 人工修订文件导入/导出
- [ ] 轻量向量检索

## M2：知识点出题

- [x] Subject Profile
- [x] 带来源上下文的 QuestionSpec 工作流
- [x] 命题、独立解答、Rubric
- [x] 多维审核和结构化仲裁
- [x] 分类重试和重试预算
- [x] 题目版本

## M3：整卷

- [x] 固定能力包和未知科目动态 ExamBlueprint
- [x] 人工蓝图确认
- [x] 每题独立并行子工作流、即时落盘和单题重跑
- [x] 整卷覆盖、难度、重复和时长审核
- [x] 三视图 LaTeX/PDF、全页检查和发布 Bundle

## M4：本地图形化工作台

- [x] 本地单用户 FastAPI + React GUI
- [x] SSE、轮询降级和断线重连
- [x] 研究、逐题、审核、Artifact 和三视图 PDF 状态
- [x] 单题 JSON 编辑、领域校验、单题重跑和 editable 重新组卷
- [ ] Blueprint 结构化编辑和版本差异
- [ ] 页面级人工验收操作

## M5：数字化和阅卷

- [ ] 试卷 PDF 到 ExamDocument
- [ ] 可编辑 LaTeX
- [ ] 无答案时生成草稿答案并等待确认
- [ ] 答卷切题和转录
- [ ] Rubric 逐项评分和证据定位
- [ ] 人工复核与审计报告
