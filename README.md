# 数据资产引擎｜经验资产化

> 让每一次 AI 对话，都沉淀成下次还能复用的经验资产。
>
> 保留决策依据、失败路径与失效边界，让经验可追溯、可复核、可长期复用。

复杂问题第一次解决，本来就需要搜索、试错和判断。真正浪费的是第二次遇到同类问题时，
又重新从零开始。

数据资产引擎把 Codex、Claude、WorkBuddy 等会话与项目过程整理成经验资产：保留决策与理由、
被否决方案、证据、风险和可复用方法，并记录来源、状态与复核日期。它不是聊天导出工具，
也不是给 AI 再加一个记忆库。

> 第一次探索是成本，第二次复用才是资产。

- Skill slug：`experience-data-asset-engine`
- 版本：`0.1.0`
- 许可：AGPL-3.0-only，提供独立商业授权

## 为什么不是另一个聊天归档或 AI 记忆工具

常见方案通常只覆盖其中一段：

| 常见方案 | 主要解决的问题 | 数据资产引擎的不同 |
|---|---|---|
| 聊天导出工具 | 把会话保存成文件 | 不只保存记录，还提炼决策、证据、失败路径和复用方法 |
| 摘要工具 | 把长对话压缩成一段文字 | 只写摘要不算完成；决策与被否决方案缺失会失败关闭 |
| AI Memory / RAG | 让 AI 下次能检索到相关内容 | 除了“找得到”，还要回答“是否可信、是否仍有效、人能否复核” |
| 自动第二大脑 | 自动维护和改写知识库 | 默认保守写入：冲突只生成待合并候选，不覆盖手写笔记，不自动判死结论 |
| 一次性归档 Skill | 完成当前这次保存 | 通过来源键与内容哈希保持幂等，管理复核日期、过时和取代关系 |

它真正优化的是：

> 不只让 AI 记得，还要让人知道这条经验来自哪里、为什么成立、什么时候应该失效。

因此它更适合已经完成过一次高成本探索、准备把结果转成长期资产的用户；不适合只想自动保存
聊天或需要实时记忆注入的场景。

当前版本原生支持 Obsidian，输出为标准 Markdown + YAML frontmatter，便于迁移。暂未提供
Logseq、Notion、思源等知识库的官方直写适配。

## 四条设计原则

1. **不写原文**：主笔记只允许短证据引用与来源指针，禁止整段复制对话。
2. **默认看不见过期内容**：`validate` / `status` 的计数与库内 `iter_notes()` 默认排除
   `已过时`、`已被取代`，需要旧语境时显式 `--include-stale` 并打印跳过数量。
   CLI 不提供搜索子命令，检索由你自己的 Obsidian 查询或 `iter_notes()` 完成。
3. **不自动判死**：日期到期只把笔记放进 `recheck --due` 待复核列表，绝不自动改状态或删除。
4. **不碰用户笔记**：只覆盖本工具创建且来源会话匹配的笔记；同名冲突写 `.pending-<hash8>.md`。

## 安装

**要求 Python 3.11 或更高版本。** 先取源码：

```bash
git clone https://github.com/1superblack/experience-data-asset-engine.git
cd experience-data-asset-engine
```

然后在仓库根目录安装：

```bash
python -m pip install -e ".[codex]"
```

- `codex` 额外依赖 `zstandard`，只有读 Codex `.jsonl.zst` 会话时才需要；不装也能处理
  `.jsonl`、`.json`、`.md`。
- 开发测试再安装 `dev`：`python -m pip install -e ".[dev,codex]"`；它提供 `pytest`、
  `coverage` 与 `ruff`。
- 不安装也可以直接用 `python scripts/cli.py ...` 运行（`scripts/cli.py` 会把 `src/` 加进
  `sys.path`）。
- 装好后多一个控制台入口 `experience-data-asset-engine`。
- 本地元数据预检：`python scripts/quick_validate.py .`，检查 `SKILL.md` 的
  frontmatter、`name`、`description` 长度、`metadata.slug/displayName/version/summary`，
  以及顶层键是否只用了官方白名单里的五个。
  它只是 **Agent Skill 的子集预检**，**不等同平台审核**。
- 官方 dry-run：`skillhub publish <zip> --dry-run` 只能证明 slug / version / displayName
  这类**字段最低兼容**，它**不校验完整文件清单、不上传、不调用服务端审核**——
  所以它通过也不等于能上架。
- 如果拿到的是分发压缩包（只有 `scripts/`、`src/`、`references/`、`agents/` 与几个根级
  Markdown），**不需要也不要执行 pip 安装**——直接用
  `python scripts/cli.py <子命令> ...` 即可；本文后面的命令把
  `experience-data-asset-engine` 换成 `python scripts/cli.py` 完全等价。

## 快速开始（2 分钟）

```bash
# 1. 指向你的 vault：目标必须是真正的 Obsidian vault（存在 .obsidian 目录；
#    普通目录会被拒绝，且不在磁盘上留下任何文件）
experience-data-asset-engine init --vault /path/to/vault --archive-dir 04-对话存档

# 2. 自检：vault、.obsidian、归档目录、状态目录、Python 版本、可选依赖
experience-data-asset-engine doctor --vault /path/to/vault

# 3. 归档一次会话（配了模型才直接写笔记；没配模型会停在 staging，见下）
experience-data-asset-engine archive --input /path/to/session.jsonl --vault /path/to/vault

# 4. 看 vault 现状
experience-data-asset-engine status --vault /path/to/vault
```

`init` 幂等：配置文件已存在时直接返回既有配置，**不覆盖**。配置固定写入
`<vault>/.experience-data-asset-engine/config.json`，跟着 vault 走，不含绝对机器路径，
不含任何密钥。重复 `init` 传不同 `--archive-dir` 不会改已有配置——要改请编辑 JSON，
或先 `uninstall` 再 `init`。

## 支持的输入

| 输入 | 后缀 | 说明 |
|---|---|---|
| Codex 会话 | `.jsonl` / `.jsonl.zst` | 读 `session_meta` / `user_message` / `agent_message` / `task_complete`；工具调用只计数不落地。`.jsonl.zst` 需要 `zstandard`，未安装时失败关闭并提示 |
| 标准化会话 | `.json` | 已经是 `NormalizedSession` 的 JSON，字段规则见 [`references/output-schema.md`](references/output-schema.md) |
| 手动整理的 Markdown | `.md` | `# 标题` + `## 角色` 版式，见下 |

### Markdown 手动导入

任意会话整理成这个形状即可导入：

```markdown
# 会话标题

## 用户

我想把归档流程做成可复用的，但不想把原文塞进知识库。

## 助手

建议只留决策、理由、被否决方案和证据，原文用来源指针代替。
```

- 角色别名中英文都认：用户 / User / Human、助手 / Assistant / Codex / Claude / AI。
- 代码块里的 `## 标题` 不算角色标题；四反引号包裹的三反引号不会提前关闭代码块。
- 缺 `# 标题`、标题前有正文、多个一级标题、无法识别的角色、空段落、未闭合围栏，
  一律失败关闭并报行号——不做猜测式解析。
- 支持 BOM / CRLF，单文件上限 2 MB。

自由格式的导出文本请先手工整理成上面的版式，否则会被拒绝。

## CLI 契约

| 命令 | 作用 | 写入行为 |
|---|---|---|
| `init --vault <path> [--archive-dir <relative-path>]` | 写入 vault 配置 | 写配置 + 建归档/状态目录，幂等 |
| `doctor [--vault <path>]` | 环境自检与诊断 | 只写探针 `.edae-probe-*.tmp` 并立即删除 |
| `archive --input <path> [--distilled <path>] [--dry-run] [--include-stale]` | 降噪 → 蒸馏 → 校验 → 写入 | 非 dry-run 时才写 Markdown / state / audit |
| `validate [--vault <path>] [--include-stale]` | 校验字段、状态、`取代自`/`取代为` 断链 | 零写入 |
| `status [--vault <path>]` | 状态分布、pending 数、断链与缺字段数 | 零写入 |
| `recheck --due [--include-stale]` | 列出到期复核候选 | 零写入，只列不改 |
| `recheck --note <path> --status <状态>` | 手改单篇状态 | 只原子替换 frontmatter 的「状态」一行 |
| `uninstall [--vault <path>] --yes` | 卸载工具自身状态 | 只删 `<vault>/.experience-data-asset-engine/` |

退出码：`0` 成功、`1` 校验或配置失败（含 vault 不存在、配置损坏、路径越界、输入后缀不认识）、
`2` 需要用户补充输入（缺参数、`recheck` 参数不成对、`uninstall` 未加 `--yes`）、
`3` 需要蒸馏（未配置模型，只生成了 staging）。

## dry-run 与 staging 的区别

这两个概念经常被混为一谈，实际完全不同：

| | `archive --dry-run` | 无模型时的 staging |
|---|---|---|
| 触发条件 | 显式加 `--dry-run` | 配了 provider 之外的情况（`provider` 为空） |
| 磁盘写入 | **一个字节都不写**，连目录都不建 | 写 `staging/<state_key>.request.md` 与 `<state_key>.schema.json` |
| 正式笔记 | 不写 | 不写，等 Agent 补完蒸馏 |
| 退出码 | `0`（即使真跑会返回 3） | `3` |
| 用途 | 先看会命中哪篇笔记、统计是什么、哈希是多少 | 把「需要蒸馏」这件事变成可交接的产物 |

dry-run 与真跑共用同一份计划（同一份覆盖后的 `noise_report`、同一个 `content_hash`），
所以 dry-run 打印的「预计 path」和「预计 action」与真跑一致；dry-run 也**不会**发起模型请求。

`staging/` 位于隐藏状态目录下，不属于知识库正文，Obsidian 不展示，也不参与状态统计。

## 笔记状态语义

| 状态 | 含义 | 是否默认可检索 |
|---|---|---|
| `有效` | 当前口径 | 是 |
| `待复核` | 到期但未发现反证，需要人工看 | 是（优先级降低） |
| `已过时` | 已被新事实否定 | 否，需 `--include-stale` |
| `已被取代` | 存在更新的同主题笔记 | 否，需 `--include-stale` |

内容时效与默认复核周期：`stable` 365 天、`evolving` 90 天、`volatile` 30 天、`progress`
14 天。时效分级只看关键词命中，证据不足一律落 `evolving`，口径见
[`references/expiry-policy.md`](references/expiry-policy.md)。

**`review_after` 是写入时的输入，不是写入时自动算出来的。** 写入端直接采用
`DistilledNote.review_after`（只校验 `YYYY-MM-DD` 格式）；周期表与
`lifecycle.compute_review_after()` 是库内工具，目前没有接入归档主链路——需要按周期重算时
由调用方自己算好再写进 `review_after`。这一版也没有 config 级的周期覆盖开关。

**过期只过滤，不删除。** `review_after` 到期只是把笔记放进 `recheck --due` 的候选列表：
工具不改状态、不移动文件、不删除任何 Markdown。状态变更只有两个来源——蒸馏时的初始值，
以及你显式执行的 `recheck --note <path> --status <状态>`。

## 数据协议

两份 JSON 协议见 [`references/output-schema.md`](references/output-schema.md)：

- `NormalizedSession`：来源无关的会话中间格式（Codex、手动导入都先落到这里）。
  `session_id` 只做逻辑标识，落盘时用 `sha256(source + ":" + session_id)` 当文件名。
- `DistilledNote`：笔记的唯一合法输入，缺 `decisions` 直接拒绝，不允许只写摘要；
  所有决策汇总后必须至少有一条非空 `rejected`（被否决方案）。
  `title` 会变成文件名、`project` 会变成目录名，两者都按「单级安全名字」校验，
  控制字符、路径分隔符、隐藏名、Windows 保留名一律拒绝。

配置字段、环境变量与诊断码见 [`references/config.md`](references/config.md)。

## 降噪与蒸馏

```python
from experience_data_asset_engine.adapters import codex_jsonl
from experience_data_asset_engine.config import load_config
from experience_data_asset_engine.distill import DistillationRequired, prepare_distillation
from experience_data_asset_engine.noise import filter_noise

session = codex_jsonl.load_codex_session(Path("rollout.jsonl.zst"))
cleaned, report = filter_noise(session)                # 四层降噪，返回新会话 + 7 项统计
note = prepare_distillation(session, load_config())    # 无模型时抛 DistillationRequired
```

- **四层降噪**：输入（系统上下文、环境注入、空白、纯确认、回显）、执行（工具角色与长日志）、
  知识（重复主题、显式失败短句）、安全（密钥、令牌、口令、Cookie、私钥 → `[已脱敏]`）。
  恒等式 `input == kept + system + tool + duplicate + dead` 由测试守住，口径见
  [`references/noise-policy.md`](references/noise-policy.md)。
- **蒸馏**：没配模型时只在 `staging/` 写请求与 schema，抛 `DistillationRequired`；
  配了模型就要求输出完整 `DistilledNote`，任何失败都失败关闭。契约见
  [`references/distill-prompt.md`](references/distill-prompt.md)。

## 写入语义

- `write_note` 只会返回 `created` / `updated` / `skipped` / `pending`，绝不静默成功。
- 同一 `source + session_id` 且内容哈希未变 → `skipped`，文件字节不动。
- 目标被手写笔记占用，或同一会话此前已归档到别的路径 → 只写
  `<title>.pending-<hash8>.md` 候选（完整 Markdown，可人工合并），不移动、不删除原笔记。
- 写入路径再次脱敏，标题与项目名脱敏后仍须通过原有路径校验。
- `created` / `updated` 用本地日期，不用 UTC——否则东八区 00:00–08:00 归档会写成前一天。

## 当前版本范围

`0.1.0` 已打通「导入 → 降噪 → 蒸馏 → 写入 → 校验 → 复核 → 卸载」这条主链路：

- **输入**：Codex `.jsonl` / `.jsonl.zst`、标准化会话 `.json`、手写 `.md`。
- **处理**：四层降噪与脱敏、模型无关蒸馏、无模型时只产出 staging。
- **写入**：原子写、内容哈希幂等、冲突落 pending 候选、单会话只允许一篇正式笔记。
- **只读命令**：`validate`、`status`、`recheck --due`。
- **打包**：`scripts/build_skill_zip.py` 可离线打出可分发的 zip（根目录含 `SKILL.md`），
  构建不联网、不读取任何 vault：

  ```bash
  python scripts/build_skill_zip.py --output dist/experience-data-asset-engine.zip
  python scripts/build_skill_zip.py --output dist/x.zip --list   # 额外打印包内清单
  ```
  同一台机器、同一 zlib 实现下两次打包字节一致；**跨机不保证同哈希**——跨机一致性请比对
  包内文件清单与内容哈希，对外发布的校验值建议由 CI 产出。
- **发布渠道**：GitHub（`1superblack/experience-data-asset-engine`）与 SkillHub。
  仓库内**不内置自动上架流水线**——具体版本是否已发布、发布到哪一版，
  以 **GitHub Releases / SkillHub 页面**的实时状态为准。`dist/` 是本机可重建的
  离线产物（已被 `.gitignore` 忽略），不是发布留存；每次构建的 SHA256 以当前构建输出为准。

## 隐私

安全边界与漏洞报告方式见 [SECURITY.md](SECURITY.md)：本工具不解密客户端数据库、
不读取真实凭据、不自动删除或覆盖用户笔记，发布前需人工审查与用户批准。

- 只读输入文件，不修改、不移动原始会话。
- 配置里只存 vault 内相对路径；API Key 只从环境变量 `EDAE_API_KEY` 读取，不落盘、不进日志。
- 密码、Token、API Key、Cookie、Authorization、私钥等敏感值在主笔记、staging 请求、
  审计与 provider 提示词中统一替换为 `[已脱敏]`；标题同样走脱敏，统计里只报次数、不报内容。
- 被降噪丢弃的文本不写入任何地方（不进 staging、不进审计、不进错误消息）。
- 不配置模型时**不发任何网络请求**，只写本地 staging 文件。
- 本项目不内附任何真实对话；仓库内 `tests/fixtures/` 下的会话样本全部为合成的测试数据，
  不是真实用户数据（分发压缩包不含 `tests/`）。

## 卸载

```bash
experience-data-asset-engine uninstall --vault /path/to/vault --yes
```

- 只删除 `<vault>/.experience-data-asset-engine/`（配置、`state.json`、`audit/`、`staging/`），
  **不动任何 Markdown 笔记**，包括本工具写过的笔记。
- 必须加 `--yes`；不加时退出码 `2` 且零删除。
- 删除前先打印将删的目录与文件清单。
- 拒绝执行的情形：目标不是真 vault（缺 `.obsidian`）、状态目录是符号链接、
  状态目录不是目录、配置缺失或无法解析。这些情况文件树零改动。
- 想连笔记一起清理：先 `uninstall`，再手动删 `archive_dir` 下的相应子目录。

## 命令行全链路

```bash
# 1. 初始化（幂等，不覆盖已有配置）
experience-data-asset-engine init --vault /path/to/vault

# 2. 归档一次会话（无模型时只写 staging，退出码 3）
experience-data-asset-engine archive --input session.md --vault /path/to/vault

# 3. 或者直接把蒸馏结果写进去（不调用模型）
experience-data-asset-engine archive --input session.md \
  --distilled note.json --vault /path/to/vault

# 4. 演算不落盘
experience-data-asset-engine archive --input session.md \
  --distilled note.json --dry-run --vault /path/to/vault

# 5. 只读检查
experience-data-asset-engine validate --vault /path/to/vault
experience-data-asset-engine status --vault /path/to/vault
experience-data-asset-engine recheck --due --vault /path/to/vault

# 6. 手改单篇状态（只动 frontmatter 的「状态」一行）
experience-data-asset-engine recheck --note "04-对话存档/项目/笔记.md" \
  --status 已过时 --vault /path/to/vault

# 7. 卸载（只删状态目录，绝不删 Markdown）
experience-data-asset-engine uninstall --vault /path/to/vault --yes
```

变更记录见 [CHANGELOG.md](CHANGELOG.md)。

## 许可证

默认开源许可为 **GNU Affero General Public License v3.0 only
（AGPL-3.0-only）**，完整条款见 [LICENSE](LICENSE)。

无法或不希望遵守 AGPL 的组织可以申请独立商业授权，适用场景和联系入口见
[COMMERCIAL-LICENSE.md](COMMERCIAL-LICENSE.md)。商业授权只在双方签署书面协议后生效。

当前版本暂不接收外部代码贡献，原因与后续贡献流程见
[CONTRIBUTING.md](CONTRIBUTING.md)。
