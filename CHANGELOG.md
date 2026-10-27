# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [0.1.0] - 2026-10-27

第一阶段（骨架与协议），不含实际归档写入。

### 命名

- 产品名统一为「数据资产引擎｜经验资产化」（品牌名：数据资产引擎）。
- 技术标识：Skill slug 与 Python 发行包 `experience-data-asset-engine`、导入包
  `experience_data_asset_engine`、CLI `experience-data-asset-engine`、
  状态目录 `.experience-data-asset-engine`、环境变量前缀 `EDAE_`。
- 原 `ai-conversation-knowledge-distiller` / `archive-to-obsidian` 标识已全部弃用。

### 新增

- Skill 元数据：`SKILL.md`（slug `experience-data-asset-engine`）、`agents/openai.yaml`。
- CLI 骨架：`init`、`doctor`，以及 `archive` / `validate` / `status` / `recheck` /
  `uninstall` 的占位入口（返回退出码 4，明确失败关闭）。
- 可移植 vault 配置：配置写入 `<vault>/.experience-data-asset-engine/config.json`，
  幂等且不覆盖已有配置；归档目录只允许 vault 内相对路径。
- `doctor` 诊断：Python 版本、vault 与 `.obsidian`、归档目录存在性与可写性、状态目录可写性、
  模型配置状态、可选依赖 `zstandard`。
- 数据协议：`NormalizedSession` / `Message` / `DistilledNote` / `NoiseReport` / `Decision`，
  含字段校验与不安全标题拒绝。
- Codex 会话适配：`.jsonl` 与 `.jsonl.zst`，提取 `session_meta`、`user_message`、
  `agent_message`、`task_complete`，工具调用只计数不落地；缺 `zstandard`、格式损坏、
  截断 JSON、无消息等情形一律失败关闭。

### 修复（Gate 1 复审）

- **init 只写真 vault**：在任何 `mkdir` / 配置写入之前校验 `.obsidian`；普通目录抛
  `ConfigError`，磁盘零新增（有「init 前后文件树一致」的负向测试）。
- **doctor 不再假绿灯**：归档目录不存在时向上找最近父目录验证可写性，可写报 `warn`
  （不是 `ok`），父路径不是目录或不可写报 `error`。
- **session_id 不进路径**：新增 `state_key = sha256(source + ":" + session_id)`；
  协议层拒绝含 `/`、`\`、`..`、绝对路径、控制字符或为空的 session_id。
- **project 按目录名校验**：拒绝绝对路径、路径分隔符、`.`/`..`、隐藏名、Windows 保留名、
  控制字符、非法字符与空格/点结尾；中文、空格、数字与常规标点仍允许。
- **title 补齐写入安全**：拒绝 C0 控制字符（换行、制表符、NUL）与 Windows 非法结尾。
- **强制被否决方案**：所有 `decisions` 的 `rejected` 全为空时校验失败，不再静默通过。
- **zstandard 真正可选**：未安装时全量测试仍通过，需要实际解压的用例自动 skip，
  「缺依赖必须失败关闭」的用例保留。
- **封住状态目录符号链接逃逸**：`<vault>/.experience-data-asset-engine` 或 `config.json`
  被换成指向 vault 外的符号链接时，`init` 与 `load_config` 在读写前就抛 `ConfigError`，
  vault 外零写入（解析后路径判定，链接套链接同样拦得住）。
- **doctor 越界即停**：归档路径越界时只报 `archive.inside_vault=error` 并立即停止，
  不再访问相对路径（原先会抛未捕获 `ValueError`）；vault 不可用或路径越界时**不写任何探针**，
  只返回结构化诊断。
- **解析失败也失败关闭**：路径解析遇到损坏链接 / 链接环抛 `OSError` 时统一转成 `ConfigError`，
  不让 `OSError` 漏到 CLI 或 doctor 外层。
- 探针文件名统一为 `.edae-probe-*.tmp`。

### 已知限制

- 尚无归档写入、降噪、蒸馏执行、检索过滤与打包发布能力。
- `quick_validate.py` 只接受 `name` / `description` / `license` / `allowed-tools` /
  `metadata` 五个顶层键，因此 `slug`、`displayName`、`version` 放在 `metadata` 下。
