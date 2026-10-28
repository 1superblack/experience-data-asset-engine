# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [1.0.0] - 2026-10-29

首个可用版本：「导入 → 降噪 → 蒸馏 → 写入 → 校验 → 复核 → 卸载」主链路全部打通，
全部通过 CLI 使用。**可离线打包**为可分发的 zip（根目录含 `SKILL.md`）。
仓库**不内置自动发布流水线**（GitHub Releases / SkillHub 上架由发布窗口手工执行），
发布状态以对应平台页面为准；`dist/` 是被 `.gitignore` 忽略的本机可重建立产物。

> 版本说明：`v0.1.0` 是一次错误版本号的预发布记录，已由正式版本 `1.0.0` 取代。

### 发布定位与构建兼容（Gate 5e / 5f）

- **对外定位定稿**：主文案为「让每一次 AI 对话，都沉淀成下次还能复用的经验资产」，
  副文案强调保留决策依据、失败路径与失效边界，让经验可追溯、可复核、可长期复用。
- **差异化明确**：不把产品描述成聊天导出、摘要或通用 AI Memory；核心差异是
  “高成本探索 → 可审计、可复核、保守写回的经验资产”，并用证据、状态、复核日期与
  取代关系管理经验生命周期。
- **当前平台边界**：原生支持 Obsidian；输出为标准 Markdown + YAML frontmatter，
  暂未提供 Logseq、Notion、思源等知识库的官方直写适配。
- **构建链修复**：SPDX `license` 元数据要求 `setuptools>=77`，构建下限从 68 收紧；
  Windows CLI 入口固定 stdout/stderr 为 UTF-8，并补构建元数据与 Windows 编码回归测试。
- **静态门禁**：dev 依赖增加 `ruff`，CI 执行 `ruff check --select F,E9`，拦住未定义名称、
  语法错误和未使用导入。
- **安装口径**：普通用户的源码安装命令收敛为 `pip install -e ".[codex]"`；
  `dev` 仅用于测试与静态检查。
- **双授权定稿**：开源许可由 MIT 调整为 `AGPL-3.0-only`；无法遵守 AGPL 的闭源商业
  集成、商业再分发或托管服务可另行申请书面商业授权。`LICENSE` 保存完整 AGPL 正文，
  `COMMERCIAL-LICENSE.md` 说明商业授权边界，`pyproject.toml` 与 Skill 元数据同步为
  `AGPL-3.0-only`。

### 发布安全与校验器契约（Gate 5b）

- **打包原子化**：先写同目录唯一临时 zip，校验通过后 `os.replace` 提交；任何失败都删临时文件、
  最终路径保持原样（原已存在的合法 zip 字节不变）。此前是「先写最终产物再校验」，
  校验失败时敏感包已落盘。
- **拒绝符号链接与路径逃逸**：根级文件、`ROOT_DIRS`、递归子文件全部检查 symlink / reparse /
  junction，并用 `resolve(strict=True)` 确认真实路径仍在仓库内，命中即 `PackageError` 失败关闭。
- **统一原子文本写入**：`config.write_text_atomic` 成为唯一实现（唯一临时文件、flush、fsync、
  `os.replace`、`BaseException` 清理），`writer` 与 `distill` 复用，删掉 `distill` 里那个
  失败不清理的固定 `.tmp`。
- **校验器契约收紧**：`displayName` 首尾空格真正生效（此前 `strip()` 后自比永远为假）、
  YAML 子集拒绝序列项/重复键/缩进跳变且报行号、输出对动态值做 `\uXXXX` 转义，
  退出码边界明确（取不到文件 2 / 内容不成立含编码非法 1）。
- **UTF-8 子进程护栏加严**：不再只看「有没有传 env」，而是确认 env 里真有
  `PYTHONUTF8=1` 与 `PYTHONIOENCODING=utf-8`，并覆盖两个测试文件。
- **文档口径**：`SKILL.md` 的 `summary` 放在 `metadata.summary`（顶层只允许官方白名单的五个键）；
  README 说明本地校验器只是子集预检、不等同平台审核；发布口径改为
  「未正式发布，`dist/` 是被 gitignore 的本机可重建立产物」。

### 兼容性收口（Gate 5c）

- **Codex 兼容**：`SKILL.md` 的 `summary` 从顶层移到 `metadata.summary`——官方校验器只接受
  `allowed-tools` / `description` / `license` / `metadata` / `name` 五个顶层键，
  顶层多一个 `summary` 就会 `Unexpected key(s)` 失败。
- **本地校验器对齐**：新增 `ALLOWED_TOP_KEYS` 白名单，未知顶层键直接失败并列出字段名；
  `metadata.summary` 纳入必填。
- **打包自检不可关闭**：删除 `build(verify=...)` 参数，自检恒执行；`main()` 对
  `OSError` / `FileExistsError` / `BadZipFile` 给结论不打 traceback。
- **护栏覆盖**：UTF-8 子进程护栏把 `tests/test_skill_metadata.py` 纳入扫描范围。

### 发布准备收口（Gate 5 复审）

- **子进程固定 UTF-8**：`tests/test_package.py` 所有 `subprocess` 调用显式注入
  `PYTHONUTF8=1` + `PYTHONIOENCODING=utf-8`，并用 AST 断言「一个都不许漏」；
  此前在普通 Windows shell（代码页 GBK）下 `test_planted_credential_fails_closed` 会因
  中文 stderr 乱码而假失败。CI 同时在 workflow 级固定这两个变量。
- **正式 Skill 校验入口**：新增 `scripts/quick_validate.py`（无第三方依赖），
  校验 frontmatter 可解析、`name` hyphen-case、description 长度上限、
  `metadata.slug/displayName/version` 合法；CI 直接调用，缺失或失败即红（不再 warning 跳过）。
- **占位联系方式清零**：`SECURITY.md` 不再写占位邮箱，改为走平台私有安全报告入口；
  并加测试防止占位地址随包发布。
- 打包产物随 `scripts/quick_validate.py` 增至 30 个文件；产物 SHA256 由打包命令输出，
  **不写进仓库**——任何一次改动都会让写死的哈希过期。

### 打包、CI 与发布准备（Gate 5）

- **离线打包**：`scripts/build_skill_zip.py` 打出根目录含 `SKILL.md` 的 zip，
  固定时间戳保证可复现（同源码两次构建 SHA256 一致）；排除 `.git`、`__pycache__`、
  `.pytest_cache`、`.coverage*`、`tests/`、`.experience-data-asset-engine/`、`dist/` 与
  `*.egg-info`；构建后自检疑似凭据并非零退出。
- **CI 工作流**：`.github/workflows/ci.yml` 覆盖 Python 3.11/3.12 与「不装 zstandard」两种环境，
  跑 `pytest -q --cache-clear`、`coverage report --fail-under=85`、`quick_validate.py`。
  **本仓库没有 remote，工作流只创建、未实际执行。**
- **SECURITY.md**：说明不自动解密客户端数据库、不读取真实凭据、不自动删除或覆盖用户笔记、
  过期只过滤不删除、发布前需人工审查与用户批准。
- **能力口径统一**：文档里「打包」与「上架」分开写——zip 打包可用，上架/发版未实现；
  反向规则进了 `tests/test_skill_metadata.py::FORBIDDEN_CLAIMS`，防止再把已实现写成未实现。

### 文档与元数据（Gate 5）

- **元数据统一**：`SKILL.md` 的 `metadata.displayName` 由「数据资产引擎」改为
  「数据资产引擎｜经验资产化」，与 `agents/openai.yaml` 的 `interface.display_name` 一致；
  `name` / `metadata.slug` 为 `experience-data-asset-engine`，`metadata.version` 为 `1.0.0`；
  最终 `license` 为 `AGPL-3.0-only`。
- **`SKILL.md` 收敛为五节**：触发条件 / 主流程 / 安全边界 / CLI 入口 / Reference 路由；
  能力边界表保留「打包发布（SkillHub / GitHub）未实现」，不夸大能力。
- **`README.md` 重写**：补 2 分钟快速开始、Codex `.jsonl` / `.jsonl.zst` 与 Markdown 手动导入
  的输入说明、`dry-run` 与 staging 的差异对照、状态语义与「过期只过滤不删除」、
  隐私与卸载说明；删除「archive 仍是退出码 4」「本版本没有写入能力」等已过期的旧陈述。
- **协议文档对齐实现**：`references/config.md` 的状态目录三行由「后续版本」改为实际使用中；
  `references/output-schema.md` 的 frontmatter 映射补 `tags` / `生成器` / `状态键`，
  并注明写入端已实现。
- **新增 `tests/test_skill_metadata.py`**：断言 frontmatter 六项元数据、slug/version/displayName
  取值、`agents/openai.yaml` 与 `SKILL.md` 的 displayName 一致、README 关键小节齐备，
  以及文档中不出现个人目录、用户名、账号等本机路径痕迹。不引入 PyYAML，frontmatter 用文本解析。
- **删掉两类未实现的承诺**（审计发现，文档与 `src/` 对不上）：
  - `references/expiry-policy.md` 原称「可在 config 里按 `policy: {volatility: days}` 覆盖周期」，
    实际 `AppConfig` 没有 `policy` 字段，改为如实说明「本版本无 config 级覆盖开关」。
  - 同文件原称「写入时 `review_after` 由 `compute_review_after()` 算出」，
    实际该函数没有任何生产调用点，`writer.py` 直接写 `DistilledNote.review_after`（只校验格式）；
    README 同步补「`review_after` 是写入时的输入，不是写入时自动算出来的」，
    并把「会自动老化管理」改为「带状态与复核日期」。
  - `SKILL.md` description 原写「按状态过滤…检索笔记」，但 CLI 没有搜索子命令，
    改为「在校验、统计时默认排除已过时与已被取代的笔记」；README 同步注明检索由
    Obsidian 查询或 `iter_notes()` 完成。
  - `CHANGELOG` 早期误称 `quick_validate.py` 不在本仓库（历史记录，现已自带
    `scripts/quick_validate.py`），相应表述改为描述校验器的键约束。
- **拆开「打包」与「上架」两种能力**（B 的 `scripts/build_skill_zip.py` 落地后，
  早期那句「没有发布产物」的笼统说法已被推翻）：
  - `打包`：本地 `python scripts/build_skill_zip.py --output dist/<name>.zip` 已可用，
    根目录直挂 `SKILL.md`、条目排序与时间戳固定、打包后回读自检，检出凭据或个人路径即非零退出。
    同机同源两次打包 zip 字节一致，**跨机不保证同哈希**（deflate 输出取决于 zlib 版本），
    文档按此口径写，未写成跨机承诺。
  - `上架发布流水线`：仍**未实现**，没有 SkillHub / GitHub 自动发布，也没有上架后一键安装路径。
  - `SKILL.md` 的「不要声称」清单同步收窄为「把包上架到 SkillHub 或 GitHub」。
- **README 隐私节链接 `SECURITY.md`**（不复制其 5 条承诺，避免两套口径）。
- `references/config.md` 的 `audit/` 一行收窄为「`created` / `updated` / `pending` 时追加；
  `skipped` 不写」——`writer.py` 的 `_append_audit` 只在 `:718`（pending）与 `:807`（成功写入）
  被调用，`skipped` 提前 return。
- 测试从「禁词」升级为「方向正确的护栏」：原先 `assert "打包发布" in text and "未实现" in text`
  会在打包能力落地后把**改对的文档判红**，现改为「上架必须标未实现 + `build_skill_zip` 必须被承认」；
  打包脚本与 `SECURITY.md` 尚在 `gate5/agent-b` 分支时对应测试自动 skip（合并后生效），
  避免本分支出现「文档引用了不存在的文件却测试通过」的假绿。

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

### CLI 全链路（Gate 4）

- **CLI 全部子命令可用**：`init` / `doctor` / `archive` / `validate` / `status` /
  `recheck` / `uninstall`，退出码收敛为 0/1/2/3；编号 `4` 已停用，没有任何子命令返回它
  （仅保留编号，避免与历史脚本冲突）。
- **archive**：按后缀识别 `.json` / `.jsonl` / `.jsonl.zst` / `.md`；
  有 provider 走「降噪 → 蒸馏 → 校验 → 写入」；无 provider 只写 staging 并返回 3；
  `--distilled` 跳过模型；`--dry-run` 不写 Markdown / state / audit / staging，连目录都不建。
- **Markdown 手动导入**（`adapters/markdown.py`）：`# 标题` + `## 角色`；
  角色别名中英文（用户/User/Human、助手/Assistant/Codex/Claude/AI）；代码块内的伪标题不算；
  无法识别的角色、缺标题、空段落、标题前正文、多个一级标题一律失败关闭并报行号；
  支持 BOM / CRLF；2MB 上限；`source` 走受控标识校验。
- **validate / status**：复用 Gate 3 的校验与扫描，零写入。
- **recheck**：`--due` 只列不改；`--note --status` 只原子替换 frontmatter 的「状态」一行，
  保留正文与其它字段，拒绝 pending 候选、非本工具笔记与越界路径。
- **uninstall**：必须 `--yes`，未加时退出码 2 且零删除；只删状态目录，绝不删 Markdown。
- **pending 命名精确匹配**：只认 `<stem>.pending-<8位小写十六进制>.md` 与旧版
  `<stem>.pending.md`；`release.pending-notes.md` 这类普通笔记不再被误判为候选。

### 修复（Gate 4 复审）

- **`--distilled` 不再自带统计**：归档前先对输入会话跑 `filter_noise`，用真实统计覆盖传入
  笔记的 `noise_report`，覆盖后的笔记才交给 writer 与 `content_hash`；
  dry-run 与真跑共用同一份计划，哈希与统计必然一致。
- **正文补「降噪统计」章节**：正式笔记与 pending 候选都带 `过滤消息 k/n`、系统/工具/重复/
  死路/脱敏六行，数字只取自 `note.noise_report`。
- **uninstall 安全边界**：目标必须是真 vault（有 `.obsidian`）、状态目录必须是普通目录、
  且必须存在可被 `load_config` 成功解析的配置；普通目录 / 缺配置 / 坏配置 / 越界配置一律拒绝，
  文件树零改动。删除前先打印将删的目录与文件清单。
- **代码清理**：删掉重复的测试辅助函数 `snapshot()`；删除空壳常量 `EXIT_NOT_IMPLEMENTED`
  （退出码收敛为 0/1/2/3）；`archive` 的真跑与 dry-run 合并到同一个 `_prepare_archive()`。

### 修复（Gate 4 终验：Markdown 围栏）

- **围栏按「字符 + 长度」匹配**：打开时记录 `fence_char` 与 `fence_length`，只有
  「同字符且长度不短于开启长度」的行才能关闭。此前把长度归一成 3，导致四反引号块里的
  三反引号被当成关闭，块内 `## 助手` 被误识别成真实角色。
- **未闭合围栏失败关闭**：到 EOF 仍未关闭时报出开启行号，不再把剩余内容当普通正文。
- **围栏开在第一条角色标题之前**：按「标题前正文」失败关闭（此前会被静默吞掉）。
- **闭合围栏不得带非空尾随文本**：opening 允许 ```` ```python ````/` ````markdown ```` 这类信息标注，
  closing 要求「同字符 + 长度足够 + 标记后只剩空格/制表符」。此前只看字符与长度，
  导致代码块内的 ` ```python ` 被当成关闭行，其后的 `## 助手` 被误识别成真实角色。
- 补 14 条回归测试：四/五反引号、波浪号嵌套、更长关闭符、围栏内三种标题形态、
  未闭合、标题前围栏、围栏内缩进保留，以及 ` ```python ` 在块内不关闭、
  作为 opening 仍能开块、关闭行尾随空白可接受、更长标记带尾随文本不关闭。

### 骨架与协议

- Skill 元数据：`SKILL.md`（slug `experience-data-asset-engine`）、`agents/openai.yaml`。
- CLI 骨架：`init`、`doctor`，以及 `archive` / `validate` / `status` / `recheck` /
  `uninstall` 的占位入口（Gate 1 阶段返回退出码 4，Gate 4 起已全部实现）。
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
  目标被手写笔记占用时只写 `<title>.pending-<hash8>.md` 候选（**完整 Markdown，可合并**，
  同一冲突重复归档命中同一个文件，不产生 `.pending-2` / `.pending-3`）；
  同一会话改标题或项目名**不再** created 第二篇正式笔记，改为落 pending 并标出「待合并到」的旧路径，
  工具不移动、不删除任何笔记；同目录临时文件 + `os.replace` 原子替换；
  `dry_run` 零写入（不建目录、不写 state/audit）。
  写入边界再次 `redact_sensitive`，标题与项目名脱敏后仍须通过原有路径校验。
- **日期口径**：`created` / `updated` 用**本地日期**，不用 UTC——否则东八区每天 00:00-08:00
  会把归档日写成前一天。
- **vault 校验与过期过滤**（`validator.py`）：`validate_vault` 结构化上报缺字段、空字段、
  字段类型错误、非法状态、非法内容时效、非法日期、`取代自`/`取代为` 断链/单向/自指、
  frontmatter 无法解析；解析失败的文件绝不进入 `valid`。`iter_notes` 默认只给
  `有效` + `待复核`（后者排序靠后）**且字段校验必须通过**——「可检索」不得比「可校验」更宽松；
  `include_stale=True` 恢复全部并如实报告跳过数。`iter_due_notes` 只产出复核候选，
  不改状态、不删文件；单篇 `review_after` 非法时跳过该篇（由校验报 `invalid_date`），
  不中断整批复核。pending 候选按文件名识别：不进检索、不算 valid、计入 `skipped`，
  也不会被当成坏 frontmatter。
- **frontmatter 往返保真**：逗号分隔的列表项按引号切分；纯数字串（如全数字哈希）渲染时加引号，
  否则回读会被解析成 `int`，导致「内容没变」被误判成「内容变了」。

### 已知限制

- **无自动发布流水线**：`scripts/build_skill_zip.py` 能离线打 zip，但 GitHub Releases 与
  SkillHub 上架靠发布窗口手工执行，仓库内没有对应 workflow；发布状态以平台页面为准，
  `dist/` 是被 `.gitignore` 忽略的本机可重建立产物。
- **Markdown 导入只支持一种版式**：`# 标题` + `## 角色`。自由格式的导出文本需要先整理成
  这个形状，否则会以「无法识别的角色标题」失败关闭（这是有意的，不做猜测式解析）。
- **pending 候选需要人工合并**：写入冲突时只落可合并的 Markdown 候选，不会自动替换或删除原笔记。
- 语义级冲突（「这个结论后来被推翻了」）不在降噪层判断，留给蒸馏阶段；
  口径见 `references/noise-policy.md`。
- Skill 元数据校验器（官方运行时的那个）只接受 `name` / `description` / `license` /
  `allowed-tools` / `metadata` 五个顶层键，因此 `slug`、`displayName`、`version`、`summary`
  都放在 `metadata` 下。本仓库自带的 `scripts/quick_validate.py` 复刻了同一套白名单。
