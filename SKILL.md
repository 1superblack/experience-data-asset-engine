---
name: experience-data-asset-engine
description: 把 AI 对话与项目过程转化为可复用、可追溯、可治理的经验数据资产，产出带时间与状态管理的 Obsidian 知识笔记。当用户要求归档对话、沉淀项目经验、整理会话记录、生成可复用经验笔记，或在校验、统计时默认排除已过时与已被取代的笔记时使用。默认不写入对话原文，不删除、不覆盖用户已有笔记。
license: AGPL-3.0-only
metadata:
  slug: experience-data-asset-engine
  displayName: 数据资产引擎｜经验资产化
  version: 0.1.0
  summary: 让每一次 AI 对话，都沉淀成下次还能复用的经验资产
---

# 数据资产引擎｜经验资产化

> 让每一次 AI 对话，都沉淀成下次还能复用的经验资产。
>
> 保留决策依据、失败路径与失效边界，让经验可追溯、可复核、可长期复用。

一份笔记只保留这几件事：当时要解决什么、查明了哪些事实、做了哪个决策以及为什么、
哪些方案被否决、证据在哪、下次能复用哪条经验。原文只留来源指针，不进知识库正文。

它不是聊天导出或 AI 记忆库，而是把已经发生的高成本探索转化成可审计、可复核的
Obsidian 经验资产：冲突只生成待合并候选，状态不会自动判死，结论可以按证据更新和取代。

## 触发条件

该用：

- 用户说「归档这段对话」「把这次讨论存进 Obsidian」「整理成可复用的经验」。
- 用户要回顾历史结论，并希望默认看不到已过时、已被取代的内容。
- 用户提供 Codex 会话文件（`.jsonl` / `.jsonl.zst`）、标准化会话 `.json`，或按版式整理好的会话 Markdown。

不该用：

- 用户只是要导出聊天原文（那是导出工具，不是资产化工具）。
- 用户只想要实时记忆注入或语义检索（应使用专门的 AI Memory / RAG 工具）。
- 用户要求自动解密客户端数据库、抓取聊天窗口或搬运凭据。
- 用户要求删除、移动或批量改写已有笔记。

## 主流程

1. `init --vault <vault>`：写入配置，创建归档与状态目录（幂等，不覆盖已有配置）。
2. `archive --input <file>`：按后缀自动识别来源（`.json` / `.jsonl` / `.jsonl.zst` / `.md`），
   做四层降噪后进入蒸馏。
   - 有模型：降噪 → 蒸馏 → 全量协议校验 → 写入 vault；
   - 无模型：只写 `staging/<state_key>.request.md` 与 `.schema.json`，退出码 3，
     **不写任何正式笔记**；Agent 补完蒸馏后用 `--distilled` 归档。
3. `archive --distilled <note.json>`：只校验并写入，不调用模型。
4. `archive --dry-run`：只演算，不写 Markdown / state / audit / staging，连目录都不建。
5. `validate` / `status` / `recheck --due`：只读，零写入。
6. `recheck --note <path> --status <状态>`：只原子改 frontmatter 的「状态」一行。
7. `uninstall --yes`：只删 `<vault>/.experience-data-asset-engine/`，绝不删 Markdown。

当前能力边界（`0.1.0`）——下表是事实，不要外推：

| 能力 | 入口 | 状态 |
|---|---|---|
| 初始化 vault 配置 | `init` | 已实现 |
| 环境自检 | `doctor` | 已实现 |
| 会话输入 | `archive --input` | 已实现 |
| 四层降噪 + 隐私脱敏 | `noise.py`（`archive` 内部调用） | 已实现 |
| 蒸馏（含无模型 staging） | `distill.py`（`archive` 内部调用） | 已实现 |
| 写入 vault（幂等 + 冲突 pending） | `writer.py` | 已实现 |
| 校验 / 状态 / 到期复核 | `validate` / `status` / `recheck` | 已实现 |
| 卸载（只删状态目录） | `uninstall --yes` | 已实现 |
| 知识库目标 | Obsidian vault（`.obsidian`） | 当前版本原生支持；输出为标准 Markdown，暂未提供其他知识库官方直写适配 |
| 离线打包（zip，根目录含 SKILL.md） | `scripts/build_skill_zip.py` | 已实现（本地构建，不联网） |
| 上架 / 发布流水线 | — | 仓库内**没有自动上架流水线**；发布状态以 GitHub Releases / SkillHub 页面为准 |

许可证默认采用 **AGPL-3.0-only**；无法遵守 AGPL 的闭源商业集成可申请独立商业授权。

**不要向用户声称做过这些没做的事**：上架 SkillHub 或发 GitHub Release、修改非本工具创建的笔记、
删除任何 Markdown、把对话原文当笔记写入。

## 安全边界

- **只写真 vault**：`init` 在任何落盘前校验 `.obsidian`；普通目录直接拒绝，磁盘零写入。
- **不写原文**：主笔记只允许短证据引用和来源指针，禁止整段复制对话。
- **不覆盖用户笔记**：同名笔记归属不明时只生成 `.pending-<hash8>.md` 候选。
- **不删除**：任何命令都不删除 Markdown；过期只在校验与统计侧过滤。
- **不出 vault**：所有写入限制在 vault 与归档子目录内，路径逃逸一律失败关闭。
- **session_id 不进路径**：幂等键统一用 `sha256(source + ":" + session_id)`。
- **不存凭据**：API Key 只从环境变量读取，不落盘；敏感值一律替换为 `[已脱敏]`，
  标题、staging 请求、provider 提示词三处口径一致。
- **失败关闭**：缺依赖、缺字段、来源格式不认识时报错退出，不降级为「原文照抄」。

## CLI 入口

```bash
python scripts/cli.py init --vault /path/to/vault
python scripts/cli.py doctor --vault /path/to/vault
python scripts/cli.py archive --input session.md [--distilled note.json] [--dry-run]
python scripts/cli.py archive --input session.md --vault /path/to/vault
python scripts/cli.py validate --vault /path/to/vault
python scripts/cli.py status --vault /path/to/vault
python scripts/cli.py recheck --due --vault /path/to/vault
python scripts/cli.py recheck --note <path> --status 已过时 --vault /path/to/vault
python scripts/cli.py uninstall --vault /path/to/vault --yes
```

安装后也可用控制台入口 `experience-data-asset-engine`。退出码：0 成功 / 1 校验或配置失败 /
2 需要补充输入 / 3 需要蒸馏。参数矩阵与诊断码见 `references/config.md`。

## Reference 路由

| 需要什么 | 读哪个文件 |
|---|---|
| 配置字段、环境变量、退出码、doctor 诊断码 | `references/config.md` |
| 会话与蒸馏笔记的 JSON 协议、校验规则 | `references/output-schema.md` |
| 四层降噪边界、7 项统计口径、脱敏规则 | `references/noise-policy.md` |
| 蒸馏提示词契约、staging 产物、失败关闭清单 | `references/distill-prompt.md` |
| 状态语义、复核周期、到期只列不改 | `references/expiry-policy.md` |
