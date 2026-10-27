# 配置说明

## 配置写在哪

```text
<vault>/.experience-data-asset-engine/config.json
```

固定位置，跟着 vault 走。目的有三个：vault 搬家后配置仍然有效；配置与笔记同源便于备份；
不含任何机器绝对路径，复制到另一台机器不会写出奇怪路径。

`.experience-data-asset-engine/` 同时存放运行期状态：

| 子路径 | 用途 | 本版本是否使用 |
|---|---|---|
| `config.json` | 配置 | 是 |
| `state.json` | 会话幂等状态（来源 + 会话 ID -> 笔记路径 + 内容哈希） | 后续版本 |
| `audit/` | 每次写入的审计 JSON | 后续版本 |
| `staging/` | 无模型时生成的蒸馏请求 | 后续版本 |

这些文件都在 `.` 开头的目录里，Obsidian 默认不展示，不会污染笔记列表。

## 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | int | 当前为 `1`。读到其它值直接失败，不做猜测性兼容。 |
| `archive_dir` | string | **vault 内相对路径**，如 `04-对话存档`。不允许绝对路径、不允许 `..`、不允许 `.` 开头的隐藏目录、不允许 vault 根目录。 |
| `provider` | string \| null | OpenAI-compatible 提供方标识。`null` 表示不直连模型。 |
| `model` | string \| null | 模型名。 |
| `base_url` | string \| null | 模型服务地址，如 `https://api.example.com/v1`。 |
| `api_key_env` | string | 存**环境变量名**，不是密钥本身。默认 `EDAE_API_KEY`。 |
| `created_at` | string \| null | 初始化时间（ISO 8601），仅作记录。 |
| `tool_version` | string \| null | 写入配置时的工具版本，仅作记录。 |

后三个字段在读取时被忽略，用于排查「这份配置是哪版工具写的」。

## 环境变量

环境变量优先级高于配置文件，适合临时切换。

| 变量 | 作用 |
|---|---|
| `EDAE_VAULT` | 指定 vault 根目录 |
| `EDAE_CONFIG` | 直接指定配置文件路径（必须仍位于 `<vault>/.experience-data-asset-engine/`） |
| `EDAE_PROVIDER` | 覆盖 `provider` |
| `EDAE_MODEL` | 覆盖 `model` |
| `EDAE_BASE_URL` | 覆盖 `base_url` |
| `EDAE_API_KEY_ENV` | 覆盖 `api_key_env` |
| `EDAE_API_KEY` | **凭据本身**，只读不写，永不落盘、永不进日志 |

## 定位顺序

`load_config()` 依次尝试：

1. 显式传入的 vault 路径（`--vault`）。
2. `EDAE_CONFIG` 指向的配置文件。
3. `EDAE_VAULT` 指向的 vault。
4. 从当前目录逐层向上查找 `.experience-data-asset-engine/config.json`。

都找不到时报「未初始化」，提示先运行 `init`。

## init 的前置条件与幂等性

**前置条件：目标目录必须是真正的 Obsidian vault（存在 `.obsidian` 目录）。**
校验发生在任何 `mkdir` / 配置写入之前——普通目录直接抛 `ConfigError`，磁盘上不新增状态目录、
不新增归档目录、不新增配置文件。这样「指错目录」不会在你机器上留垃圾。

**状态目录与配置文件也必须落在 vault 内（按解析后的真实路径判断）。**
如果 `<vault>/.experience-data-asset-engine` 或其中的 `config.json` 被预先换成指向 vault 外的
符号链接，`init` 与 `load_config` 都会在读写之前抛 `ConfigError`，vault 外零写入。
判定用的是 `Path.resolve()` 后的路径，因此「链接、链接套链接、大小写差异」都绕不过去。

- 配置已存在：**不覆盖**，直接返回既有配置，CLI 打印「配置已存在，未覆盖」。
- 配置不存在：写入 `config.json`（临时文件 + 原子替换），创建归档目录与状态目录。
- 重复 `init` 传入不同 `--archive-dir` 不会改变已有配置——要改请手动编辑 JSON，
  或先 `uninstall` 再 `init`（`uninstall` 只删状态目录，不删笔记）。

## doctor 诊断码

| 诊断码 | 取值 | 含义 |
|---|---|---|
| `python.version` | ok / error | Python 是否 >= 3.11 |
| `vault.exists` | ok / error | vault 目录是否存在 |
| `vault.obsidian` | ok / error | 是否存在 `.obsidian`（判断是不是真 vault） |
| `archive.inside_vault` | ok / error | 归档目录是否落在 vault 内 |
| `archive.exists` | ok / warn / error | 归档目录存在 / 待创建 / 被同名文件占用 |
| `archive.writable` | ok / warn / error | 目录已存在时探针写入是否成功；**目录不存在时向上找最近的父目录验证**，父目录可写报 warn（不是 ok），父目录不是目录或不可写报 error，无法验证报 error |

越界与不可用时的短路规则（不写探针）：

| 情形 | 输出 |
|---|---|
| 归档路径越界 | 只输出 `archive.inside_vault=error` 后**立即停止**，不访问相对路径、不写探针；`state.writable` 标记为「已跳过」 |
| 状态目录越界 | `state.writable=error`（状态目录越界，拒绝写入），不写探针 |
| vault 不存在 / 缺 `.obsidian` | `vault.exists` 或 `vault.obsidian` 报 error，`archive.writable` 与 `state.writable` 均标记为「已跳过」 |

`archive.writable` 的判定细则（防止假绿灯）：

| 情形 | 级别 | 含义 |
|---|---|---|
| 归档目录存在且探针写入成功 | ok | 可以直接写 |
| 归档目录不存在，最近父目录可写 | warn | 首次归档时会创建，尚未实际验证该目录本身 |
| 归档目录不存在，最近父路径不是目录 | error | 创建会失败 |
| 归档目录不存在，最近父目录不可写 | error | 创建会失败 |
| 归档目录不存在，找不到任何存在的父路径 | error | 无法验证 |
| `state.writable` | ok / error | 状态目录是否可写（缺失视为 error，提示重新 init） |
| `model.provider` | ok / warn | 是否配置了模型提供方 |
| `model.credentials` | ok / warn | 配置了提供方时，`api_key_env` 指向的环境变量是否存在 |
| `codex.zstandard` | ok / warn | 是否可读 `.jsonl.zst` |

`doctor` 有任一项 `error` 时退出码为 `1`；只有 `warn` 时退出码为 `0`。
`doctor` 是只读的：只在状态目录与归档目录写一个 `.edae-probe-*.tmp` 探针文件并立即删除。
**vault 不存在、缺少 `.obsidian`、或归档路径越界时，一个探针都不写**——对应检查直接跳过，
只返回结构化诊断。

## 退出码

| 码 | 含义 |
|---|---|
| `0` | 成功 |
| `1` | 校验或配置失败（含 vault 不存在、配置损坏、路径越界） |
| `2` | 需要用户补充输入（缺 `--vault`、未知子命令、无子命令） |
| `3` | 需要蒸馏（预留：未配置模型时由 `archive` 返回） |
| `4` | 子命令尚未实现 |

## 安全边界

- **只写真 vault**：`init` 在任何落盘动作前校验 `.obsidian`；普通目录直接拒绝，零写入。
- 归档目录只能是 vault 内相对路径；`../x`、绝对路径、隐藏目录、vault 根目录全部拒绝。
- 路径校验先 `resolve()` 再判断，符号链接跳出 vault 同样被拒绝。
- 配置里没有密钥字段；`api_key()` 只读环境变量。
