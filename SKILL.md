---
name: ai-conversation-knowledge-distiller
description: 把 AI 对话与会话记录蒸馏为可追溯、带时间与状态管理的 Obsidian 知识笔记，而不是把聊天原文搬进知识库。当用户要求归档对话、把聊天沉淀成知识库、整理会话记录、生成可复用经验笔记，或需要按状态过滤（排除已过时、已被取代）检索笔记时使用。默认不写入对话原文，不删除、不覆盖用户已有笔记。
license: MIT
metadata:
  slug: ai-conversation-knowledge-distiller
  displayName: 对话知识蒸馏器
  version: 0.1.0
---

# AI 对话知识蒸馏器

## 目标

把一次对话压缩成一份可复用、可追溯、会自动老化的知识笔记：背景、诊断、决策与理由、
被否决方案、证据、可复用经验、风险、待办。原文只作为来源指针保留，不进知识库正文。

## 何时使用

- 用户说「归档这段对话」「把这次讨论存进 Obsidian」「整理成可复用的经验」。
- 用户要回顾历史结论，并希望默认看不到已过时、已被取代的内容。
- 用户提供一个 Codex 会话文件（`.jsonl` / `.jsonl.zst`）或手动整理的会话 Markdown。

以下情况不要使用：

- 用户只是要导出聊天原文（那是导出工具，不是蒸馏器）。
- 用户要求自动解密客户端数据库、抓取聊天窗口或搬运凭据。
- 用户要求删除、移动或批量改写已有笔记。

## 当前实现状态（0.1.0）

本版本只交付第一阶段的骨架，可用于**配置 vault、自检环境、校验输入数据协议**。已实现：

| 能力 | 入口 | 状态 |
|---|---|---|
| 初始化 vault 配置 | `cli.py init` | 已实现 |
| 环境自检 | `cli.py doctor` | 已实现 |
| 标准化会话协议 | `adapters/normalized_json.py` | 已实现 |
| Codex JSONL / `.jsonl.zst` 输入 | `adapters/codex_jsonl.py` | 已实现 |
| 蒸馏笔记协议 | `models.py` | 已实现 |
| 归档写入、检索过滤、复核 | `archive` / `validate` / `status` / `recheck` / `uninstall` | 未实现，返回退出码 4 |

调用未实现的子命令时，脚本会明确提示「尚未实现」。**不要向用户声称归档已经完成。**

## 主流程

1. `doctor` 读环境，确认 vault、归档目录、状态目录可用；已有配置时不要再 `init`。
2. 用 `adapters` 把来源会话标准化为 `NormalizedSession`（字段见
   `references/output-schema.md`）。
3. 按蒸馏协议产出 `DistilledNote` JSON：每个决策必须带理由，至少写清一个被否决方案。
4. 交给写入端（后续版本）落盘；当前版本只做协议校验。

## CLI 入口

```bash
python scripts/cli.py --version
python scripts/cli.py init --vault /path/to/vault --archive-dir 04-对话存档
python scripts/cli.py doctor
python scripts/cli.py doctor --vault /path/to/vault
```

安装后也可用控制台入口 `obsidian-knowledge-distiller`。

## 安全边界

- **不写原文**：主笔记只允许短证据引用和来源指针，禁止整段复制对话。
- **不覆盖用户笔记**：同名笔记归属不明时只生成 `.pending.md`。
- **不删除**：任何命令都不删除 Markdown；过期只做检索端过滤。
- **不出 vault**：所有写入限制在 vault 与归档子目录内，路径逃逸一律失败关闭。
- **不存凭据**：API Key 只从环境变量读取，不落盘；敏感值一律替换为 `[已脱敏]`。
- **失败关闭**：缺依赖、缺字段、来源格式不认识时报错退出，不降级为「原文照抄」。

## Reference 路由

| 需要什么 | 读哪个文件 |
|---|---|
| 配置字段、环境变量、退出码、doctor 诊断码 | `references/config.md` |
| 会话与蒸馏笔记的 JSON 协议、校验规则 | `references/output-schema.md` |
