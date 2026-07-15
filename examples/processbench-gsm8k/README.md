# ProcessBench GSM8K 过程验证 Pilot

本目录保存 Assessment Workbench 对公开 [Qwen/ProcessBench](https://huggingface.co/datasets/Qwen/ProcessBench) 的首个真实过程验证实验。ProcessBench 采用 Apache-2.0 许可证，任务不是判断最终答案，而是定位数学推理中的**第一处错误步骤**；`-1` 表示整条推理过程正确。

## 数据切片

- 来源 split：`gsm8k`
- 公开原始 split：400 条，其中 193 条过程正确、207 条过程错误
- 本目录诊断切片：24 条
- 过程正确：9 条
- 过程错误：15 条
- 最终答案正确但过程存在错误：6 条

该切片使用 `diagnostic` 策略，刻意提高 lucky-answer case 的占比，用于检查 Verifier 是否会被正确最终答案掩盖。它不是从 400 条总体中均匀抽样，因此不能作为总体排行榜结果。

## Gemini Flash 实际结果

模型：`gemini-3.5-flash`，temperature 0，trial 1。

| 指标 | 结果 |
| --- | ---: |
| 全 case 第一处错误精确匹配 | 20 / 24 = **83.3%** |
| 错误过程检出率 | 11 / 15 = **73.3%** |
| 错误过程第一步精确定位率 | 11 / 15 = **73.3%** |
| 正确过程接受率 | 9 / 9 = **100%** |
| 最终答案正确但过程错误的 trap 定位率 | 2 / 6 = **33.3%** |

模型定位出的 11 条错误全部命中了正确步骤，因此“已检出 case 的平均步骤偏移”为 0；其主要失败模式不是偏移一两步，而是把 4 条 lucky-answer 过程整体误判为完全正确。

并发 8 首轮完成 23 / 24 条；一条响应被本地 Gemini 网关截断。恢复运行只补跑缺失 case，没有重复调用已完成样本。

## 一个真实漏检

`gsm8k-290` 的最终答案 `40` 正确，但第 0 步文字声称 Zack 的柜子是 Peter 的“两倍”，同一步计算却执行了除以 `1/4` 并得到 `20`。ProcessBench 将第 0 步标为首个错误；Gemini Flash 返回 `-1`，认为全部步骤正确。

这个 case 说明仅检查最终答案或整体计算结果会漏掉局部自相矛盾的推理。它与 Reward Hacking 的关系在于：候选轨迹可以通过给出正确结论获得高奖励，同时在中间过程保留无效陈述。

## 文件

- `cases.jsonl`：从公开 ProcessBench 导入的 24 条过程 case，保留来源、许可证、问题、步骤和第一处错误标注。
- `observations.gemini-flash.jsonl`：24 条真实模型判断，不包含 Oracle 泄漏。
- `report.gemini-flash.json`：离线计算的过程检测与定位指标。

## 复现

下载公开 split：

```powershell
curl.exe -L -o processbench-gsm8k.json `
  https://huggingface.co/datasets/Qwen/ProcessBench/resolve/main/gsm8k.json
```

导入诊断切片：

```powershell
uv run assessment-workbench benchmark import-processbench `
  --source processbench-gsm8k.json `
  --split gsm8k `
  --output examples/processbench-gsm8k/cases.jsonl `
  --limit 24 `
  --sampling diagnostic
```

执行 Oracle-blind 过程验证并生成报告：

```powershell
uv run assessment-workbench benchmark observe-process `
  --cases examples/processbench-gsm8k/cases.jsonl `
  --output examples/processbench-gsm8k/observations.gemini-flash.jsonl `
  --verifier gemini_flash `
  --model gemini-3.5-flash `
  --trial 1 `
  --concurrency 8

uv run assessment-workbench benchmark report-process `
  --cases examples/processbench-gsm8k/cases.jsonl `
  --observations examples/processbench-gsm8k/observations.gemini-flash.jsonl `
  --verifier gemini_flash `
  --trial 1 `
  --output examples/processbench-gsm8k/report.gemini-flash.json
```

## 证据边界

该实验可以证明：ProcessBench 数据已经进入统一、可恢复、Oracle-blind 的 Verifier 评测链路，并暴露了 Gemini Flash 在 lucky-answer 过程上的明显弱点。

该实验不能证明：模型在完整 400 条 GSM8K split、MATH/OlympiadBench/Omni-MATH split、多模型、多随机种子或自适应攻击上具有相同表现。
