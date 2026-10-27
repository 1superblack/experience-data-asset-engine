# 数据协议

两份 JSON 协议，`schema_version` 当前为 `1`。字段增删必须同时升版本，读取端遇到未知版本
直接失败，不做猜测性兼容。

- `NormalizedSession`：来源无关的会话中间格式，所有适配器的输出。
- `DistilledNote`：笔记的唯一合法输入，写入端只认这一份。

## 1. NormalizedSession

```json
{
  "schema_version": 1,
  "source": "codex",
  "session_id": "019abc",
  "title": "会话标题",
  "created_at": "2026-10-27T10:00:00+08:00",
  "updated_at": "2026-10-27T11:00:00+08:00",
  "model": "demo-model",
  "cwd": "/path/to/project",
  "messages": [
    {
      "role": "user",
      "text": "只保留有效对话内容",
      "created_at": "2026-10-27T10:01:00+08:00",
      "kind": "message"
    }
  ],
  "metadata": {}
}
```

| 字段 | 类型 | 必填 | 规则 |
|---|---|---|---|
| `schema_version` | int | 是 | 必须等于 `1` |
| `source` | string | 是 | 来源标识，非空。已知值：`codex`、`chatgpt`、`claude`、`workbuddy`、`manual` |
| `session_id` | string | 是 | 来源内的唯一会话 ID，**逻辑标识，不是路径**。非空、<= 200 字符、不含控制字符、不含 `/` 或 `\`、不是 `.`/`..`、不是绝对路径。文件名一律用 `sha256(source + ":" + session_id)` |
| `title` | string | 是 | 会话标题，非空 |
| `created_at` | string | 是 | ISO 8601，带时区更好，允许无时区 |
| `updated_at` | string | 是 | ISO 8601 |
| `messages` | array | 是 | 可以是空数组，但适配器通常要求至少 1 条 |
| `metadata` | object | 否 | 适配器附加信息（工具调用计数、被忽略记录数等） |
| `model` | string \| null | 否 | 会话使用的模型 |
| `cwd` | string \| null | 否 | 会话工作目录。**只是记录，不用于任何读写决策** |

### session_id 与幂等键

`session_id` 只做标识，**永远不直接拼进路径**。写盘时统一用：

```python
state_key = sha256(f"{source}:{session_id}").hexdigest()   # 64 位十六进制
```

`NormalizedSession.state_key` 就是这个值。这样 `../../outside` 这类 ID 在协议层就被拒绝，
即便被放过，落盘时也只会得到一个安全的十六进制文件名。

### Message

| 字段 | 类型 | 必填 | 取值 |
|---|---|---|---|
| `role` | string | 是 | `user` / `assistant` / `system` / `tool` |
| `text` | string | 是 | 正文，允许空串（空消息交由降噪层处理） |
| `created_at` | string \| null | 否 | ISO 8601 |
| `kind` | string | 否 | `message`（默认）/ `tool_call` / `tool_result` / `system` / `summary` |

## 2. DistilledNote

```json
{
  "schema_version": 1,
  "title": "主题短名",
  "project": "项目名",
  "summary": "一句话结论",
  "background": "目标与背景",
  "diagnosis": ["诊断事实"],
  "decisions": [
    {
      "decision": "采用方案 A",
      "reason": "因为证据 X",
      "rejected": ["方案 B：成本过高"]
    }
  ],
  "evidence": ["可复核的数据或短引用"],
  "reusable": ["可复用经验"],
  "risks": ["风险"],
  "todo": ["待办"],
  "noise_report": {
    "input_messages": 210,
    "kept_messages": 83,
    "system_noise": 42,
    "tool_noise": 55,
    "duplicate_topics": 4,
    "dead_ends": 6,
    "redactions": 3
  },
  "content_volatility": "evolving",
  "status": "有效",
  "review_after": "2027-01-25"
}
```

| 字段 | 类型 | 必填 | 规则 |
|---|---|---|---|
| `schema_version` | int | 否 | 缺省等于 `1`；给了就必须等于 `1` |
| `title` | string | 是 | 会变成文件名：不能含尖括号、冒号、引号、斜杠、反斜杠、问号、星号、竖线；不能含 C0 控制字符（换行、制表符、NUL 等）；不能以 `.` 开头；不能以空格或点结尾；不能是 Windows 保留名（`CON`、`PRN`、`AUX`、`NUL`、`COM1-9`、`LPT1-9`）；长度 <= 120；首尾无空白 |
| `project` | string | 是 | 会变成归档**目录名**，只允许单级目录名：不能含 `/` `\`、不是绝对路径、不是 `.`/`..`、不以 `.` 开头、不含 `<>:"\|?*`、不以空格或点结尾、不是 Windows 保留名、不含控制字符、长度 <= 80。中文、空格、数字与常规标点允许 |
| `summary` | string | 是 | 一句话结论 |
| `background` | string | 是 | 目标与背景，写「为什么要做」 |
| `diagnosis` | string[] | 是 | 诊断事实，只写查明的东西，不写推测；可空数组 |
| `decisions` | object[] | 是 | **不能为空**。每项 `decision` + `reason` 都不能为空；**所有 decisions 汇总后至少要有一条有效 `rejected`**（可写 `无备选：<实质原因>`，原因 >= 8 个有效字符且非占位话术） |
| `evidence` | string[] | 是 | 可复核的证据或短引用，**不允许整段粘贴对话** |
| `reusable` | string[] | 是 | 可复用经验 |
| `risks` | string[] | 是 | 风险；可空数组 |
| `todo` | string[] | 是 | 待办；可空数组 |
| `noise_report` | object | 是 | 见下，7 个计数器全部必填 |
| `content_volatility` | string | 是 | `stable` / `evolving` / `volatile` / `progress` |
| `status` | string | 是 | `有效` / `待复核` / `已过时` / `已被取代` |
| `review_after` | string | 是 | `YYYY-MM-DD`，由生命周期策略计算，不允许自由填 |

### NoiseReport

| 计数器 | 含义 |
|---|---|
| `input_messages` | 输入消息总数 |
| `kept_messages` | 保留进蒸馏的有效消息数，必须 <= `input_messages` |
| `system_noise` | 被过滤的输入噪音（系统角色、环境注入、空白、纯确认、连续回显） |
| `tool_noise` | 被过滤的执行噪音（工具角色/调用/输出、长日志式粘贴） |
| `duplicate_topics` | 被合并的重复主题数 |
| `dead_ends` | 被剔除的显式失败短句 |
| `redactions` | 脱敏替换**次数**（按实际替换计数） |

所有计数器为非负整数，且满足不变式：

```text
input_messages == kept_messages + system_noise + tool_noise + duplicate_topics + dead_ends
```

`redactions` 独立于该等式——它统计替换次数，不减少消息数。规则细节见
[`noise-policy.md`](noise-policy.md)。

**`noise_report` 只能由管道产生。** 走模型时提示词要求模型抄写一遍，但解析成功后会被
`filter_noise` 的真实统计覆盖：模型自报的数字一律不采信，只为让输出结构完整。

### 为什么 `decisions` 不能为空

只写摘要的产物不是蒸馏结果，是摘要。摘要在几周后无法回答「当时为什么不选另一个方案」，
而这正是知识库要解决的问题。校验层因此把 `decisions` 设为硬性条件。

### 为什么必须至少写一条被否决方案

`rejected` 不是可选装饰。记录「本来还能怎么做、为什么没这么做」，是知识笔记与会议纪要的
分界线：它保存的是判断边界，而不是结论本身。因此校验规则是：

> 所有 `decisions` 汇总后，至少要有 **一条有效** `rejected`，否则整份笔记被判为不合格。

单个决策可以没有 `rejected`（有些小步决策确实没有备选），但整份笔记一条都没有，说明蒸馏端
没有真的还原当时的取舍过程。这条规则是硬性的，不允许用空数组绕过。

**确实没有备选时**，允许写豁免项 `无备选：<实质原因>`（中文冒号或半角冒号都接受），
但原因必须实质，否则等于绕过校验：

| 判定 | 例子 |
|---|---|
| ✅ 通过 | `无备选：上游接口只提供一个可用端点，且没有可替换实现。` |
| ❌ 失败 | `无备选：`（没有原因） |
| ❌ 失败 | `无备选：原因未知` / `无备选：不适用` / `无备选：暂无` / `无备选：略` / `无备选：TODO` / `无备选：待定` |
| ❌ 失败 | 冒号后不足 **8 个有效字符**（不计空白） |

选择「允许豁免但要求理由」而不是「强制编造备选」，是因为后者会逼模型为了过校验虚构被否决方案，
反而破坏知识真实性。豁免项同样算满足「至少一条有效 `rejected`」。

`diagnosis` / `evidence` / `reusable` / `risks` / `todo` 允许空数组，但渲染时章节标题
必须存在，空的写「无」。允许为空是因为「这次没有风险」「没有待办」是合法结论。

## 3. 与 Obsidian frontmatter 的映射

写入端（后续版本）按此映射生成 frontmatter，不改字段名：

| DistilledNote | frontmatter |
|---|---|
| `project` | `项目` + 目录名 |
| `title` | `主题` + 文件名 |
| `status` | `状态` |
| `content_volatility` | `内容时效`（`stable` → 稳定，`evolving` → 演进，`volatile` → 易变，`progress` → 进度） |
| `review_after` | `review_after` |
| — | `created` / `updated` / `valid_from`（写入当天） |
| — | `取代自` / `取代为`（双向链接） |
| — | `来源` / `会话ID` / `内容哈希` |

## 4. 失败关闭清单

以下输入一律报错退出，不降级、不猜测：

- JSON 语法错误（报行号列号）。
- 顶层不是对象。
- `schema_version` 不是 `1`。
- 必填字段缺失、类型不对、空字符串。
- `role` / `kind` / `status` / `content_volatility` 不在枚举内。
- 时间戳不是 ISO 8601、`review_after` 不是 `YYYY-MM-DD`。
- `session_id` 含路径分隔符、控制字符、`.`/`..`、绝对路径、超长或为空。
- `project` 含路径分隔符、控制字符、隐藏名、保留名、非法字符、以空格或点结尾、超长或为空。
- `title` 含控制字符（换行/制表符/NUL）、非法字符、隐藏名、保留名、以空格或点结尾、超长。
- 所有 `decisions` 的 `rejected` 都为空（或只写了「无备选：」而不给原因、或原因只是「原因未知 / 不适用 / 暂无」等占位话术）。
- `noise_report` 缺计数器、有负数、`kept_messages > input_messages`。

## 5. 版本策略

- 加字段（向后兼容）：升次版本号，`schema_version` 不变。
- 改字段含义 / 删字段 / 改枚举：升 `schema_version`，旧版本读取直接失败。
- 每个 Gate 结束前跑一次全量 `pytest -q`，协议测试是唯一防线。
