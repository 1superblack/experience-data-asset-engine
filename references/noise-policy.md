# 降噪与脱敏策略

目标只有一个：**进入蒸馏提示词的，只能是有信息量的对话内容**。系统上下文、工具大输出、凭据
一次都不许进。所有丢弃都要能对账。

## 1. 四层边界

| 层 | 计数器 | 处理什么 | 明确不处理什么 |
|---|---|---|---|
| 输入噪音 | `system_noise` | system 角色、环境注入标记、空白消息、纯确认语、连续回显 | 不判断内容是否切题 |
| 执行噪音 | `tool_noise` | `tool` 角色、`tool_call` / `tool_result` 类型、长日志式粘贴 | 不解析工具参数语义 |
| 知识噪音 | `duplicate_topics` / `dead_ends` | 确定性重复、显式失败短句 | **不判断「结论是否被推翻」**，语义冲突交给 Agent 蒸馏阶段 |
| 安全噪音 | `redactions` | 密钥、令牌、口令、Cookie、Authorization、私钥块 | 不做 NLP 实体识别 |

### 1.1 输入噪音（system_noise）

命任一条即丢：

- `role == "system"` 或 `kind == "system"`。
- 包含环境注入标记：`<app-context>`、`</app-context>`、`<environment_context>`、
  `<user_info>`、`<system-reminder>`、`<permissions>`。
- 以 `# Skills instructions`、`Permission instructions`、`system prompt` 开头。
- 含 `This is the system prompt` / `following skills are available`。
- 归一化后为空（纯空白）。
- 整条就是确认语：`好 / 好的 / 嗯 / 收到 / 可以 / 行 / ok / OK / 明白 / 了解 / 继续 / 谢谢`。
- 与前一条消息文本完全相同（归一化后比较）——**连续回显**。

取舍：注入标记只要出现就整条丢弃（宁可少留）。如果要在对话里讨论这些标记，请用代码块包住，
但仍然可能被丢——这是为了不让系统上下文漏进知识库而付的代价。

### 1.2 执行噪音（tool_noise）

命任一条即丢：

- `role == "tool"`，或 `kind in {"tool_call", "tool_result"}`。
- 长度 >= 600 字符，且含以下之一：`Traceback (most recent call last):`、`exit code: N`、
  行首 `$ `、整条就是一个代码块、同时出现 `stdout` 与 `stderr`。

**不丢**的情况：用户手贴的短报错（< 600 字符）。那是人提供的证据，不是工具噪声。

### 1.3 知识噪音（duplicate_topics / dead_ends）

`duplicate_topics`：

- 归一化（压缩空白 + 小写）后完全相同；或
- 两条长度比 >= 0.9，且 `difflib.SequenceMatcher` 相似度 >= 0.9（比较前 400 字符）。

用相似度而不是「前 N 字符相同」，是因为短文本的尾部差异会让前缀规则失效
（「…不能进路径（第一版）」和「…（第二版）」前缀正好覆盖全文）。

`dead_ends`：

- 消息 **<= 30 字符**，且以显式失败语开头：`失败 / 报错 / 还是不行 / 仍然失败 / 又失败 /
  不行 / 没用 / 无效 / 白试 / 放弃 / 跑不起来 / 不生效 / 没效果 / 走不通 / 死路 / 用都没有 /
  试了…不行 / 尝试…失败 / failed / doesn't work / no luck`；
- 且**不含**转折或结论词：`因为 / 所以 / 原因 / 结论 / 改成 / 改为 / 修复 / 解决 / 最后 /
  于是 / 不过 / 但是 / 其实 / 发现 / 换成`。

所以「失败了，不过我发现是并发写入导致的」会被保留——那是复盘，不是死路。

### 1.4 安全噪音（redactions）

按顺序替换，先命中的先替换，后续规则看到的是已替换文本，因此不会重复计数：

| 顺序 | 形态 | 结果 |
|---|---|---|
| 1 | `-----BEGIN … PRIVATE KEY----- … -----END … PRIVATE KEY-----` | `[已脱敏]` |
| 2 | `sk-` 开头的长串（>= 12 位） | `[已脱敏]` |
| 3 | `Bearer <token>` | `Bearer [已脱敏]` |
| 4 | `Cookie: …` 整行 | `Cookie: [已脱敏]` |
| 5 | `Authorization: …` 整行 | `Authorization: [已脱敏]` |
| 6 | `password= / passwd= / pwd=` 后跟值 | `password=[已脱敏]` |
| 7 | `api_key= / access_token= / refresh_token= / token= / secret= / client_secret= / secret_key=` | `键=[已脱敏]` |

私钥必须排第一：否则内部的 base64 会被后续规则切碎，替换后残留片段还可能被当成普通文本留下。

`redactions` 统计的是 **subn 的真实替换次数**，不是「命中了几条规则」，也不是「扫到了几个可疑词」。

## 2. 判定优先级

```text
输入噪音 -> 执行噪音 -> 重复主题 -> 死路 -> 保留
```

一条消息只进一个计数器，因此恒有：

```text
input_messages == kept_messages + system_noise + tool_noise + duplicate_topics + dead_ends
```

这个不变式在 `test_counter_invariant_holds` 与 200 条样本测试里被断言。
`redactions` 独立于该等式——它统计替换次数，不减少消息数。

## 3. 截断（不是过滤）

保留消息超过 **4,000 字符**会截断为前 4,000 字符 + `…[已截断]`。

- 截断发生在脱敏**之后**：先替换密钥，再截断，避免把密钥切成两半后无法匹配。
- 截断不减少消息数，不计入七个计数器。它只是防止单条消息撑爆提示词。

## 4. 只读与轨迹

- `filter_noise(session)` 返回新的 `NormalizedSession`，原对象与原始输入文件都不变。
- 被丢弃的文本**不写入任何地方**（不进 staging、不进审计、不进错误消息）。
- 保留消息里的敏感值全部是 `[已脱敏]`；计数只报次数，不报内容。

## 5. 明确不做的

| 不做 | 原因 | 什么时候做 |
|---|---|---|
| 判断「这个结论后来被推翻了」 | 需要理解语义，不是确定性规则 | Agent 蒸馏阶段（在 `diagnosis` / `decisions` 里体现） |
| 判断两段讨论是否同一主题（语义级） | 同上 | Agent 蒸馏阶段 |
| 代码块内文本特殊处理 | 会引入「代码块里可以是密钥」的漏洞 | 不做，也不打算做 |
| 按 token 数动态裁剪 | 需要分词器依赖，MVP 不引入 | 视实际提示词长度再定 |

## 6. 200 条合成样本基线

`tests/fixtures/noisy-session-200.json`（全合成，无真实对话）：

```text
input_messages    200
kept_messages      45
system_noise       55   （注入 25 + 确认/空白 15 + 连续回显 15）
tool_noise         50   （25 组 tool_call + tool_result，其中 5 条输出 5000+ 字符）
duplicate_topics   30
dead_ends          20
redactions          4
```

- 输入正文合计约 48.7k 字符，保留正文约 1.4k 字符（压缩比约 35:1）。
- 保留消息里不含 `<app-context>`、`Traceback (most recent call last)`、`sk-`、任何口令原值。
- 断言写在 `tests/test_noise.py::test_200_message_sample_*`。
