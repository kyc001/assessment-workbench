# ProcessBench GSM8K 过程验证实验

本目录保存 Assessment Workbench 对公开 [Qwen/ProcessBench](https://huggingface.co/datasets/Qwen/ProcessBench) GSM8K split 的真实过程验证结果。ProcessBench 使用 Apache-2.0 许可证；任务是定位数学解答中的**第一处错误步骤**，`-1` 表示全过程正确。

## 全量实验

实验设置：

- split：GSM8K 全量 400 条；
- Verifier：`gemini-3.5-flash`；
- temperature：0；
- trial：1；
- Oracle-blind：模型输入只含题目和编号步骤，不含 `first_error_step` 或 `final_answer_correct`；
- 执行：增量写入、按 `(verifier, trial, case_id)` 断点续跑。

数据集组成：

| 类型 | 数量 |
| --- | ---: |
| 全部 Case | 400 |
| 正确过程 | 193 |
| 错误过程 | 207 |
| 最终答案正确但过程错误 | 7 |

Gemini Flash 结果：

| 指标 | 结果 |
| --- | ---: |
| 全 Case 首错精确匹配 | 364 / 400 = **91.0%** |
| 错误过程检出率 | 203 / 207 = **98.1%** |
| 错误过程首错精确定位率 | 174 / 207 = **84.1%** |
| 正确过程接受率 | 190 / 193 = **98.4%** |
| 最终答案正确但过程错误的 trap 精确定位率 | 3 / 7 = **42.9%** |
| 已检出错误上的平均步骤偏差 | **0.256** |

Verifier 漏检了 4 条错误过程，并在另外 29 条错误过程上选错了第一处错误位置；它还错误拒绝了 3 条正确过程。7 条 lucky-answer trap 中只有 3 条被精确定位，说明正确结论仍会显著干扰局部过程验证。

## 两个具体 Case

### `gsm8k-0`：成功定位

题目从 18 只粉色火烈鸟开始，其中 6 只被涂成白色，随后又加入 18 只粉色。候选解答先正确得到 12 只粉色和 6 只白色，却紧接着写成：

> “Sue has `12 + 6 = 18` pink flamingos and 6 white flamingos.”

`18` 是总数，不是粉色数量。ProcessBench 将步骤 1 标为首错；Gemini 同样预测步骤 1，并明确指出混淆了总数与粉色数量，confidence 为 `1.0`。

### `gsm8k-290`：正确答案掩盖过程错误

Peter 的柜子是 Zack 的四分之一，Peter 的体积为 5。候选解答计算 `5 / (1/4) = 20`，却在同一句中称 Zack 只是 Peter 的“twice as big”；随后又正确得到 Timothy 的体积为 40。

算式使用四倍，文字却声称两倍。ProcessBench 将步骤 0 标为首错；Gemini 返回 `-1`、confidence `1.0`，并声称全部步骤正确。该样本属于“最终答案正确、局部推理陈述无效”的 process-reward trap。

完整题目、逐步候选解答、Oracle 与 Gemini 原始 rationale 在仓库根目录 [README](../../README.md) 中展开。

## 文件

| 文件 | 内容 |
| --- | --- |
| `cases.full.jsonl` | 全量 400 条公开 ProcessBench Case |
| `observations.gemini-flash.full.jsonl` | 400 条真实 Gemini Flash 判断 |
| `report.gemini-flash.full.json` | 全量离线指标 |
| `cases.jsonl` | 24 条 diagnostic pilot |
| `observations.gemini-flash.jsonl` | pilot 的模型判断 |
| `report.gemini-flash.json` | pilot 指标 |

24 条 pilot 曾用于优先暴露 lucky-answer 弱点，不是总体均匀抽样；项目结论应以 400 条全量结果为主。

## 复现

下载公开 split：

```powershell
curl.exe -L -o processbench-gsm8k.json `
  https://huggingface.co/datasets/Qwen/ProcessBench/resolve/main/gsm8k.json
```

导入全部 Case：

```powershell
uv run assessment-workbench benchmark import-processbench `
  --source processbench-gsm8k.json `
  --split gsm8k `
  --output examples/processbench-gsm8k/cases.full.jsonl
```

运行 Oracle-blind Verifier。输出文件已存在时，命令只补跑缺失 Case：

```powershell
uv run assessment-workbench benchmark observe-process `
  --cases examples/processbench-gsm8k/cases.full.jsonl `
  --output examples/processbench-gsm8k/observations.gemini-flash.full.jsonl `
  --verifier gemini_flash `
  --model gemini-3.5-flash `
  --trial 1 `
  --concurrency 4 `
  --request-delay 2 `
  --workspace workspaces/processbench-gemini
```

生成报告：

```powershell
uv run assessment-workbench benchmark report-process `
  --cases examples/processbench-gsm8k/cases.full.jsonl `
  --observations examples/processbench-gsm8k/observations.gemini-flash.full.jsonl `
  --verifier gemini_flash `
  --trial 1 `
  --output examples/processbench-gsm8k/report.gemini-flash.full.json
```

## 证据边界

该实验可以证明：完整 ProcessBench GSM8K 已进入统一、可恢复、Oracle-blind 的 Verifier 评测链路，并量化了 Gemini Flash 的首错定位与 lucky-answer 弱点。

该实验不能证明：模型在其他 ProcessBench split、多模型、多随机种子或自适应攻击上具有相同表现；也不能证明任何 RL 训练或 Reward-Hacking 缓解效果。
