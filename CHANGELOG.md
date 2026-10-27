# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [0.1.0] - 2026-10-27

第一阶段（骨架、协议、降噪与蒸馏），不含实际归档写入。

### 命名

- 产品名统一为「数据资产引擎｜经验资产化」（品牌名：数据资产引擎）。
- 技术标识：Skill slug 与 Python 发行包 `experience-data-asset-engine`、导入包
  `experience_data_asset_engine`、CLI `experience-data-asset-engine`、
  状态目录 `.experience-data-asset-engine`、环境变量前缀 `EDAE_`。
- 原 `ai-conversation-knowledge-distiller` / `archive-to-obsidian` 标识已全部弃用。

### 新增

- **四层降噪与脱敏**（`noise.py`）：输入噪音（系统角色、环境注入、空白、纯确认、连续回显）、
  执行噪音（工具角色/调用/输出、长日志）、知识噪音（确定性重复 + 显式失败短句）、
  安全噪音（密钥/令牌/口令/Cookie/Authorization/私钥 → `[已脱敏]`）。
  返回新会话与 7 项统计，且满足 `input == kept + system + tool + duplicate + dead`。
  单条 > 4,000 字符截断（脱敏之后截断）。
- **蒸馏执行器**（`distill.py`）：`prepare_distillation` / `validate_distilled_note` /
  `note_json_schema`。无模型时只在 `staging/` 写 `<state_key>.request.md` 与 `.schema.json`
  并抛 `DistillationRequired`（退出码 3）；有模型时走 OpenAI-compatible `chat/completions`，
  输出必须通过全量协议校验，模型自报的 `noise_report` 会被真实统计覆盖。
- 200 条合成噪声样本（`tests/fixtures/noisy-session-200.json`）：200 → 保留 45，
  系统 55 / 工具 50 / 重复 30 / 死路 20 / 脱敏 4，保留正文约 1.4k 字符。
- 文档：`references/noise-policy.md`（四层边界与统计口径）、
  `references/distill-prompt.md`（提示词契约与失败关闭清单）。

### 骨架与协议

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
- **解析失败也失败关闭**：所有边界判断统一走 `resolve_safely()`——捕获 `OSError`（链接损坏、
  Windows `WinError 1921`）与 `RuntimeError`（`Symlink loop`），并对每个路径组件逐跳
  `readlink` 显式检测链接环（非严格 `resolve()` 在 Windows 上会**静默**返回环路径而不抛错，
  只靠异常判断会漏）；成环、层数超 32、解析失败统一转 `ConfigError`。
  CLI 遇到链接环返回退出码 1，stderr 输出普通「错误: … outside vault」，不打印 traceback。
- 探针文件名统一为 `.edae-probe-*.tmp`。

### 修复（Gate 2 第二轮复审）

- **`source` 不再是无约束自由文本**：它原先只要求非空，却会被直接写进 staging 的「来源」行、
  provider 提示词的「来源：…」以及 `state_key` 的哈希输入——因此可以携带密钥、个人路径、
  换行或提示注入内容，成为第二个绕过脱敏的元数据旁路。现在 `source` 是**受控标识符**：
  只允许小写字母、数字、点、下划线、连字符，必须以字母或数字开头，长度 1–40；
  写入前先 `strip()` + 转小写；非法值在 `NormalizedSession` 校验阶段就失败，
  流不到 staging / provider / `state_key`。需要展示名称请放 `metadata`。
  已知值 `codex` / `chatgpt` / `claude` / `workbuddy` / `manual` 保持兼容，
  扩展值如 `obsidian-exporter` / `cli.import` 同样合法。

### 修复（Gate 2 复审）

- **标题不再绕过脱敏**：`filter_noise` 对 `session.title` 执行 `redact_sensitive`，次数并入
  `redactions`，派生会话使用脱敏后的 title；staging 的「标题」行与 provider 提示词只能读到
  脱敏值，原 `NormalizedSession` 保持不变。此前标题里的 `sk-*`、口令、`Bearer` 会原样进入
  `staging/<state_key>.request.md` 与模型请求。
- **截断私钥也脱敏**：原规则只匹配完整的 `BEGIN … END` 块，缺 END 的残片会被原样保留。
  现新增一条「从 BEGIN 到文本末尾」的规则，宁可多吃也不留 base64 残片；覆盖
  `PRIVATE KEY` / `RSA` / `OPENSSH` / `EC` / `DSA` 等标题。
- **重叠规则不再重复计数**：规则顺序调整为「先字段级/整行，再裸 token」——
  `Authorization: Bearer x` 从计 2 次改为计 1 次；`password=sk-…`、`api_key=sk-…` 同理。
  `api_key/token/...` 组内对「值以 `Bearer` 开头」做排除，避免把字段名吃掉却留下真令牌。
- **被否决方案改为 A 策略（硬化版）**：允许写 `无备选：<实质原因>`，但原因须 >= 8 个有效字符
  且不得是「原因未知 / 不适用 / 暂无 / 略 / TODO / 待定」等占位话术；占位写法仍然整体判不合格。
  选 A 而不是强制给备选，是因为后者会逼模型虚构被否决方案，破坏知识真实性。

### 新增（Gate 3：生命周期、安全写入与 vault 校验）

- **生命周期**（`lifecycle.py`）：`compute_review_after` / `is_searchable` / `is_due` /
  `classify_volatility`。复核周期 `stable 365 / evolving 90 / volatile 30 / progress 14` 天；
  `有效`、`待复核` 默认可检索，`已过时`、`已被取代` 默认不可检索；
  时效分级保守确定性，证据不足一律落 `evolving`。口径见 `references/expiry-policy.md`。
- **安全写入**（`writer.py`）：`write_note` 返回 `created / updated / skipped / pending`，
  绝不静默成功。同一 `source + session_id` 内容未变即 `skipped`，文件字节不动；
  目标被手写笔记占用时只写 `<title>.pending.md`（冲突再加哈希后缀，12 个名额用尽即失败关闭）；
  同目录临时文件 + `os.replace` 原子替换；`dry_run` 零写入（不建目录、不写 state/audit）。
  写入边界再次 `redact_sensitive`，标题与项目名脱敏后仍须通过原有路径校验。
- **vault 校验与过期过滤**（`validator.py`）：`validate_vault` 结构化上报缺字段、空字段、
  字段类型错误、非法状态、非法内容时效、非法日期、`取代自`/`取代为` 断链/单向/自指、
  frontmatter 无法解析；解析失败的文件绝不进入 `valid`。`iter_notes` 默认只给
  `有效` + `待复核`（后者排序靠后），`include_stale=True` 恢复全部并如实报告跳过数。
  `iter_due_notes` 只产出复核候选，不改状态、不删文件。
- **frontmatter 往返保真**：逗号分隔的列表项按引号切分；纯数字串（如全数字哈希）渲染时加引号，
  否则回读会被解析成 `int`，导致「内容没变」被误判成「内容变了」。

### 已知限制

- **CLI 尚未接上**：`archive` / `validate` / `status` / `recheck` / `uninstall` 仍是占位，
  返回退出码 4。`write_note` / `validate_vault` / `iter_notes` 目前只能通过 Python 调用。
- 降噪与蒸馏目前只能**通过 Python 调用**（`filter_noise` / `prepare_distillation`），
  还没有接进 CLI 的 `archive` 流程。
- 语义级冲突（「这个结论后来被推翻了」）不在降噪层判断，留给蒸馏阶段；
  口径见 `references/noise-policy.md`。
- `quick_validate.py` 只接受 `name` / `description` / `license` / `allowed-tools` /
  `metadata` 五个顶层键，因此 `slug`、`displayName`、`version` 放在 `metadata` 下。
