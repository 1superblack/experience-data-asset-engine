# Obsidian 对话知识蒸馏器

把 AI 对话蒸馏成**可复用、可追溯、会自动老化管理**的 Obsidian 知识笔记，而不是把聊天原文
搬进知识库。

一个对话结束后，真正有价值的通常只有几件事：当时要解决什么、查明了哪些事实、做了哪个决策
以及为什么、哪些方案被否决、证据在哪、下次能复用哪条经验。其余都是过程噪声。本工具只保留
这几件事，并给每条笔记标上时间、状态、复核日期和取代关系。

## 四条设计原则

1. **不写原文**：主笔记只允许短证据引用与来源指针，禁止整段复制对话。
2. **默认看不见过期内容**：检索端默认排除 `已过时`、`已被取代`，需要旧语境时显式
   `--include-stale` 并打印跳过数量。
3. **不自动判死**：日期到期只把笔记放进 `recheck --due` 待复核列表，绝不自动改状态或删除。
4. **不碰用户笔记**：只覆盖本工具创建且来源会话匹配的笔记；同名冲突写 `.pending.md`。

## 安装

把仓库放到本地任意目录，在仓库根目录执行：

```bash
python -m pip install -e ".[dev,codex]"
```

- 要求 Python 3.11+。
- `codex` 额外依赖 `zstandard`，只有读 Codex `.jsonl.zst` 会话时才需要。
- 不安装也可以直接用 `python scripts/cli.py ...` 运行。

## 快速开始

```bash
# 1. 指向你的 vault（目标必须是真正的 Obsidian vault：存在 .obsidian 目录；
#    普通目录会被拒绝，且不在磁盘上留下任何文件）
python scripts/cli.py init --vault /path/to/vault --archive-dir 04-对话存档

# 2. 自检：vault、归档目录、状态目录、Python 版本、可选依赖
python scripts/cli.py doctor

# 3. 查看版本
python scripts/cli.py --version
```

`init` 幂等：配置文件已存在时直接返回既有配置，**不覆盖**。配置固定写入
`<vault>/.obsidian-knowledge-distiller/config.json`，跟着 vault 走，不含绝对机器路径，
不含任何密钥。

## CLI 契约

| 命令 | 作用 | 本版本状态 |
|---|---|---|
| `init --vault <path> [--archive-dir <relative-path>]` | 写入 vault 配置 | 可用 |
| `doctor [--vault <path>]` | 环境自检与诊断 | 可用 |
| `archive --input <path> [--dry-run]` | 蒸馏并写入笔记 | 未实现 |
| `validate --vault <path>` | 校验 frontmatter、状态、断链 | 未实现 |
| `status` | 有效/待复核/已过时/已被取代统计 | 未实现 |
| `recheck --due` / `recheck --note <path> --status <状态>` | 到期复核 | 未实现 |
| `uninstall` | 只删除工具自身状态目录 | 未实现 |

退出码：`0` 成功、`1` 校验或配置失败、`2` 需要用户补充输入、`3` 需要蒸馏（未配置模型）、
`4` 子命令尚未实现。

## 笔记状态语义

| 状态 | 含义 | 是否默认可检索 |
|---|---|---|
| `有效` | 当前口径 | 是 |
| `待复核` | 到期但未发现反证，需要人工看 | 是（优先级降低） |
| `已过时` | 已被新事实否定 | 否，需 `--include-stale` |
| `已被取代` | 存在更新的同主题笔记 | 否，需 `--include-stale` |

内容时效与默认复核周期：`stable` 365 天、`evolving` 90 天、`volatile` 30 天、`progress`
14 天。到期只标记待复核。

## 数据协议

两份 JSON 协议见 [`references/output-schema.md`](references/output-schema.md)：

- `NormalizedSession`：来源无关的会话中间格式（Codex、手动导出都先落到这里）。
  `session_id` 只做逻辑标识，落盘时用 `sha256(source + ":" + session_id)` 当文件名。
- `DistilledNote`：笔记的唯一合法输入，缺 `decisions` 直接拒绝，不允许只写摘要；
  所有决策汇总后必须至少有一条非空 `rejected`（被否决方案）。
  `title` 会变成文件名、`project` 会变成目录名，两者都按「单级安全名字」校验，
  控制字符、路径分隔符、隐藏名、Windows 保留名一律拒绝。

配置字段、环境变量与诊断码见 [`references/config.md`](references/config.md)。

## 隐私

- 只读输入文件，不修改、不移动原始会话。
- 配置里只存 vault 内相对路径；API Key 只从环境变量读，不落盘。
- 密码、Token、API Key、Cookie 等敏感字段在主笔记、审计、日志中统一替换为 `[已脱敏]`。
- 本项目不内附任何真实对话样本，测试数据全部为合成。

## 当前版本范围

`0.1.0` 只交付第一阶段骨架：CLI 骨架、vault 配置与 `doctor`、标准化会话与蒸馏笔记协议、
Codex `.jsonl` / `.jsonl.zst` 输入适配。降噪、蒸馏、写入、检索、打包发布在后续阶段实现，
见 [CHANGELOG.md](CHANGELOG.md)。**在对应能力实现前，`archive` 会以退出码 4 失败关闭。**

## 卸载

```bash
python scripts/cli.py uninstall
```

只删除 `<vault>/.obsidian-knowledge-distiller/`（配置、状态、审计、staging），不动任何
Markdown 笔记。

## 许可证

MIT，见 [LICENSE](LICENSE)。
