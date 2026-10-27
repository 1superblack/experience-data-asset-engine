# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [0.1.0] - 2026-10-27

第一阶段（骨架与协议），不含实际归档写入。

### 新增

- Skill 元数据：`SKILL.md`（slug `ai-conversation-knowledge-distiller`）、`agents/openai.yaml`。
- CLI 骨架：`init`、`doctor`，以及 `archive` / `validate` / `status` / `recheck` /
  `uninstall` 的占位入口（返回退出码 4，明确失败关闭）。
- 可移植 vault 配置：配置写入 `<vault>/.obsidian-knowledge-distiller/config.json`，
  幂等且不覆盖已有配置；归档目录只允许 vault 内相对路径。
- `doctor` 诊断：Python 版本、vault 与 `.obsidian`、归档目录存在性与可写性、状态目录可写性、
  模型配置状态、可选依赖 `zstandard`。
- 数据协议：`NormalizedSession` / `Message` / `DistilledNote` / `NoiseReport` / `Decision`，
  含字段校验与不安全标题拒绝。
- Codex 会话适配：`.jsonl` 与 `.jsonl.zst`，提取 `session_meta`、`user_message`、
  `agent_message`、`task_complete`，工具调用只计数不落地；缺 `zstandard`、格式损坏、
  截断 JSON、无消息等情形一律失败关闭。

### 已知限制

- 尚无归档写入、降噪、蒸馏执行、检索过滤与打包发布能力。
- `quick_validate.py` 只接受 `name` / `description` / `license` / `allowed-tools` /
  `metadata` 五个顶层键，因此 `slug`、`displayName`、`version` 放在 `metadata` 下。
