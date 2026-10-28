# 数据资产引擎｜经验资产化

> 把 AI 对话与项目过程转化为可复用、可追溯、可治理的经验数据资产。

把 AI 对话蒸馏成**可复用、可追溯、带状态与复核日期**的 Obsidian 知识笔记，而不是把聊天原文
搬进知识库。

一个对话结束后，真正有价值的通常只有几件事：当时要解决什么、查明了哪些事实、做了哪个决策
以及为什么、哪些方案被否决、证据在哪、下次能复用哪条经验。其余都是过程噪声。本工具只保留
这几件事，并给每条笔记标上时间、状态、复核日期和取代关系。

- Skill slug：`experience-data-asset-engine`
- 版本：`0.1.0`
- 许可：MIT

## 四条设计原则

1. **不写原文**：主笔记只允许短证据引用与来源指针，禁止整段复制对话。
2. **默认看不见过期内容**：`validate` / `status` 的计数与库内 `iter_notes()` 默认排除
   `已过时`、`已被取代`，需要旧语境时显式 `--include-stale` 并打印跳过数量。
   CLI 不提供搜索子命令，检索由你自己的 Obsidian 查询或 `iter_notes()` 完成。
3. **不自动判死**：日期到期只把笔记放进 `recheck --due` 待复核列表，绝不自动改状态或删除。
4. **不碰用户笔记**：只覆盖本工具创建且来源会话匹配的笔记；同名冲突写 `.pending-<hash8>.md`。

## 安装

**要求 Python 3.11 或更高版本。** 从源码仓库安装，在仓库根目录执行：

```bash
python -m pip install -e ".[dev,codex]"
```

- `codex` 额外依赖 `zstandard`，只有读 Codex `.jsonl.zst` 会话时才需要；不装也能处理
  `.jsonl`、`.json`、`.md`。
- `dev` 装 `pytest` 与 `coverage`，只跑测试才需要。
- 不安装也可以直接用 `python scripts/cli.py ...` 运行（`scripts/cli.py` 会把 `src/` 加进
  `sys.path`）。
- 装好后多一个控制台入口 `experience-data-asset-engine`。
- 本地元数据预检：`python scripts/quick_validate.py .`，检查 `SKILL.md` 的
  frontmatter、`name`、`description` 长度、`metadata.slug/displayName/version`。
  它只是 **Agent Skill 的子集预检**，**不等同平台审核**；真实平台兼容性以官方
  `skillhub publish --dry-run` 为准。
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
- **未正式发布**：没有上架 SkillHub，也没有 GitHub Release。`dist/` 是本机可重建的
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

MIT，见 [LICENSE](LICENSE)。
