# 蒸馏契约

这份文件描述**提示词里放了什么、要求模型产出什么**。不包含任何真实会话，示例数据全部是合成内容。

## 1. 请求结构

`POST {base_url}/chat/completions`，OpenAI-compatible：

| 字段 | 值 |
|---|---|
| `model` | `config.model` |
| `temperature` | `0`（同一输入要可复现） |
| `response_format` | `{"type": "json_object"}`（OpenAI 兼容实现可忽略） |
| `messages[0]` | `role=system`，内容为下面的「蒸馏契约」原文 |
| `messages[1]` | `role=user`，内容为：会话标题、来源、降噪统计、过滤后的对话 |
| Header | `Authorization: Bearer <EDAE_API_KEY>`，密钥只在 Header，不写日志、不进异常消息 |

进入 `messages[1]` 的对话**已经是降噪后的**：系统上下文、工具输出、重复、死路都被丢弃，
凭据已替换为 `[已脱敏]`，单条超过 4,000 字符已截断。总量上限 40,000 字符，超出会写明省略。

## 2. system 契约原文

```text
你是一个知识蒸馏器，不是摘要器。把对话压缩成一份可复用、可追溯的经验数据资产。

硬性要求：
1. 只输出一个 JSON 对象，不要输出解释、不要输出 Markdown 围栏之外的任何文字。
2. 必须包含字段：summary、background、diagnosis、decisions、evidence、reusable、
   risks、todo、noise_report、content_volatility、status、review_after，
   以及 title、project、schema_version。
3. 每个 decision 必须有非空 reason（为什么这么选）。
4. 所有 decisions 汇总后，至少要有 1 条非空 rejected（被否决方案：本来还能怎么做、为什么没做）。
   如果当时确实只有一条路，就写「无备选：原因…」。
5. summary 是一句话结论，不能替代其它字段；只写摘要的输出会被判为不合格。
6. evidence 只放可复核的证据或短引用，禁止整段粘贴对话原文。
7. content_volatility 取 stable / evolving / volatile / progress 之一。
8. status 取 有效 / 待复核 / 已过时 / 已被取代 之一。
9. review_after 用 YYYY-MM-DD。
10. noise_report 的七个数字必须原样复制下面给出的统计，不要自己编。
```

契约文本在 `src/experience_data_asset_engine/distill.py::DISTILL_CONTRACT`，此处与之逐字一致；
改契约必须同时改这两处，否则 staging 请求与真实请求会不一致。

## 3. 期望输出

一个 JSON 对象，字段与 `references/output-schema.md` 的 `DistilledNote` 完全一致：

| 字段 | 约束 |
|---|---|
| `title` | 会成为文件名：不能含非法字符/控制字符、不能以点开头、<= 120 字 |
| `project` | 会成为目录名：单级安全名，<= 80 字 |
| `summary` | 一句话结论 |
| `background` | 目标与背景 |
| `diagnosis` | 诊断事实数组（可空） |
| `decisions` | 至少 1 条；每条 `decision` + `reason` 非空；**全体汇总至少 1 条非空 `rejected`** |
| `evidence` | 可复核证据数组，禁止整段原文 |
| `reusable` | 可复用经验数组（可空） |
| `risks` | 风险数组（可空） |
| `todo` | 待办数组（可空） |
| `noise_report` | 七个计数器，管道会用真实统计覆盖 |
| `content_volatility` | `stable` / `evolving` / `volatile` / `progress` |
| `status` | `有效` / `待复核` / `已过时` / `已被取代` |
| `review_after` | `YYYY-MM-DD` |

## 4. 无 provider 时的 staging

不配置模型时**不调用任何网络**，只写两个文件到
`<vault>/.experience-data-asset-engine/staging/`：

| 文件 | 内容 |
|---|---|
| `<state_key>.request.md` | 上面第 1 节的完整请求（契约 + 统计 + 过滤后对话 + 输出要求） |
| `<state_key>.schema.json` | `DistilledNote` 的 JSON Schema |

`state_key = sha256(source + ":" + session_id)`，原始 `session_id` 不进文件名。
随后抛 `DistillationRequired`（`exit_code = 3`），调用方应转交 Agent 或人工完成蒸馏，
把结果写成 `<state_key>.distilled.json` 再交给写入端。

**staging 目录不是归档目录**：它位于隐藏状态目录下，不属于知识库正文，也不会被 Obsidian 展示。

## 5. 失败关闭清单

任何一种情况都直接抛错，**绝不降级为「把原文写进笔记」**：

| 情况 | 异常 |
|---|---|
| 配了 provider 但 `EDAE_API_KEY` 为空 | `ConfigError` |
| 配了 provider 但 `base_url` 为空 | `ConfigError` |
| HTTP 非 2xx | `DistillationError`（只报状态码） |
| 网络失败 / 超时 | `DistillationError` |
| 响应不是 JSON / 超过 2MB | `DistillationError` |
| `choices` 缺失或为空、`message.content` 为空 | `DistillationError` |
| `finish_reason == "length"`（截断） | `DistillationError` |
| 内容不是合法 JSON | `DistillationError`（报行号列号，不回显内容） |
| 缺字段、只有 summary、类型错、枚举越界、`rejected` 全空 | `SchemaError` |

异常消息里只出现错误类型、状态码、行列号，不出现密钥、完整响应或完整会话。

## 6. 统计不可由模型决定

`noise_report` 是管道产物（`filter_noise` 的返回值），模型没有能力知道真实计数。
因此流程是「要求模型抄一遍 → 解析成功 → **用真实统计覆盖**」。模型给错数字不会导致失败，
但也不会被采信；模型省略该字段会被协议校验直接拒绝。
