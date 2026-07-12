# MinerU 集成

MinerU 是可选的独立第三方解析组件，不随本项目默认安装。它负责将 PDF、PPTX、DOCX、图片等转换成结构化内容；本项目负责课程语义、知识网络、出题和阅卷。

## 推荐部署

生产方向推荐 MinerU API 或 `mineru-router`，本项目调用其 HTTP 接口。这样可以将 GPU、模型和解析依赖隔离在独立服务中。

当前适配器兼容 MinerU 的同步 `/file_parse` 模式：

```bash
assessment-workbench materials ingest lecture.pdf \
  --course physics-2026 \
  --kind lecture \
  --parser mineru-api
```

地址通过环境变量设置：

```text
AW_MINERU_API_URL=http://127.0.0.1:8000
```

## 本地 CLI

安装 MinerU 后可以使用：

```bash
assessment-workbench materials ingest lecture.pdf \
  --course physics-2026 \
  --kind lecture \
  --parser mineru-cli
```

适配器调用：

```text
mineru -p <source> -o <temporary-output>
```

并读取 `content_list.json`。临时目录会在规范化后清理。

## 规范化边界

MinerU 输出被转换为稳定的 `ParsedDocument`：

- 页码统一为 1-based
- 公式转换为 `EquationBlock`
- 表格转换为 `TableBlock`
- 图片标题转换为 `ImageBlock`
- 标题保留层级路径
- 每个 block 获得稳定 ID

MinerU 的原始字段不会泄漏到命题工作流，只保留在 block metadata 中用于诊断。

## 长课程材料

一学期材料应按文件增量导入，不应合并后一次传给模型：

1. 文件哈希和解析缓存
2. 各文件独立失败和重试
3. 课程图谱增量合并
4. 删除材料时只重建受影响关系
5. 所有知识点保留源文件和页码

后续会接入 MinerU 的异步 `POST /tasks`，用于大文件和批量材料。

## 许可证

MinerU 使用其自己的 MinerU Open Source License。本项目不复制或再分发 MinerU 源码；用户选择接入时需单独遵守其许可证。
