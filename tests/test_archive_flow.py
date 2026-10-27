"""归档准备的单一降噪口径（Gate 4）。

锁定四件事：

1. 一次归档只跑一次 ``filter_noise``；
2. ``--distilled`` / provider / staging 三条路径的统计都来自这一次；
3. 原始会话不被修改，dry-run 与真跑算出同一个 ``content_hash``；
4. 正式笔记与 pending 候选的「降噪统计」章节都取 ``note.noise_report``。

假模型服务只监听 127.0.0.1 随机端口，不访问任何真实服务。
"""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from experience_data_asset_engine import noise, writer
from experience_data_asset_engine.adapters.normalized_json import load_normalized_json
from experience_data_asset_engine.archive_flow import prepare_archive
from experience_data_asset_engine.config import (
    DEFAULT_ARCHIVE_REL,
    STATE_DIRNAME,
    AppConfig,
    read_json_file,
)
from experience_data_asset_engine.distill import (
    DistillationRequired,
    prepare_distillation,
    validate_distilled_note,
)
from experience_data_asset_engine.models import DistilledNote, NormalizedSession

FIXTURES = Path(__file__).resolve().parent / "fixtures"
NOISY = FIXTURES / "noisy-session.json"
DISTILLED = FIXTURES / "distilled-e2e.json"
ENVELOPE = json.loads((FIXTURES / "distilled.json").read_text(encoding="utf-8"))

#: noisy-session.json 里真实会被保留的 / 总消息数。只断言这两个数，
#: 其余计数不写死——口径一旦变动应该由 noise 的测试去覆盖。
NOISY_KEPT = 3
NOISY_INPUT = 10


# --- 构造器与工具 ------------------------------------------------------------


def noisy_session() -> NormalizedSession:
    return load_normalized_json(NOISY)


def supplied_note() -> DistilledNote:
    """``--distilled`` 用的外部笔记：自带 6/6 的统计，必须被管道覆盖。"""
    return validate_distilled_note(read_json_file(DISTILLED))


def snapshot(root: Path) -> dict[str, bytes]:
    """目录下所有文件的相对路径 -> 字节，用于断言「一个字节都没写」。"""
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class FakeProvider:
    """最小假模型服务：记录请求体，返回固定 envelope。"""

    def __init__(self, payload: dict):
        self.payload = payload
        self.requests: list[bytes] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "FakeProvider":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                outer.requests.append(self.rfile.read(length))
                data = json.dumps(outer.payload, ensure_ascii=False).encode("utf-8")
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
                    pass

            def log_message(self, *args) -> None:  # noqa: A002
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self._thread.start()
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}/v1"
        return self

    def __exit__(self, *exc_info) -> None:
        assert self._server is not None
        self._server.shutdown()
        self._server.server_close()


def with_provider(config: AppConfig, base_url: str) -> AppConfig:
    return replace(config, provider="openai-compatible", model="demo-model", base_url=base_url)


@pytest.fixture
def base_config(tmp_path: Path) -> AppConfig:
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    state_dir = vault / STATE_DIRNAME
    state_dir.mkdir()
    archive_dir = vault / DEFAULT_ARCHIVE_REL
    archive_dir.mkdir()
    return AppConfig(
        vault=vault.resolve(),
        archive_dir=archive_dir.resolve(),
        state_dir=state_dir.resolve(),
    )


@pytest.fixture
def counted_filter(monkeypatch: pytest.MonkeyPatch) -> list[NormalizedSession]:
    """真实的计数包装：替换 **模块属性**，因此 distill 与 archive_flow 都算在内。

    两个模块都以 ``noise_module.filter_noise`` 的形式调用，故这里替换 ``noise``
    上的属性才会真实生效——只 patch 某个模块里的绑定名会漏计。
    """
    real = noise.filter_noise
    calls: list[NormalizedSession] = []

    def wrapper(session: NormalizedSession):
        calls.append(session)
        return real(session)

    monkeypatch.setattr(noise, "filter_noise", wrapper)
    return calls


# --- 统计口径 ---------------------------------------------------------------


def test_noisy_fixture_reports_three_of_ten():
    """先钉住样本本身：真实降噪结果就是保留 3 / 输入 10。"""
    _cleaned, report = noise.filter_noise(noisy_session())
    assert report.kept_messages == NOISY_KEPT
    assert report.input_messages == NOISY_INPUT


def test_supplied_note_stats_are_overwritten_by_pipeline(base_config: AppConfig):
    included = supplied_note()
    assert (included.noise_report.kept_messages, included.noise_report.input_messages) == (6, 6)

    plan = prepare_archive(noisy_session(), base_config, distilled_note=included)

    assert plan.mode == "distilled"
    assert plan.distilled and not plan.staging_required and not plan.provider_planned
    assert plan.note is not None
    assert plan.note.noise_report == plan.report
    assert (plan.report.kept_messages, plan.report.input_messages) == (NOISY_KEPT, NOISY_INPUT)
    # 传入笔记自带的 6/6 不得残留
    assert plan.note.noise_report.to_dict() == plan.report.to_dict()
    assert plan.note.to_dict()["noise_report"]["kept_messages"] != 6


def test_stats_match_across_distilled_provider_and_staging(
    base_config: AppConfig, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("EDAE_API_KEY", "sk-test-only")
    session = noisy_session()
    baseline = noise.filter_noise(session)[1]

    staging = prepare_archive(session, base_config)
    assert staging.mode == "staging"
    assert staging.report == baseline

    distilled = prepare_archive(session, base_config, distilled_note=supplied_note())
    assert distilled.report == baseline

    with FakeProvider(ENVELOPE) as provider:
        provider_plan = prepare_archive(session, with_provider(base_config, provider.base_url))

    assert provider_plan.mode == "provider"
    assert provider_plan.note is not None
    assert provider_plan.report == baseline
    assert provider_plan.note.noise_report == baseline


# --- 一次归档只跑一次降噪 ----------------------------------------------------


def test_archive_runs_filter_noise_once_per_path(
    base_config: AppConfig, counted_filter: list[NormalizedSession], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("EDAE_API_KEY", "sk-test-only")
    session = noisy_session()

    prepare_archive(session, base_config, distilled_note=supplied_note())
    assert len(counted_filter) == 1, "distilled 路径只允许一次降噪"
    counted_filter.clear()

    prepare_archive(session, base_config)
    assert len(counted_filter) == 1, "staging 路径只允许一次降噪"
    counted_filter.clear()

    with FakeProvider(ENVELOPE) as provider:
        prepare_archive(session, with_provider(base_config, provider.base_url))
    assert len(counted_filter) == 1, "provider 路径只允许一次降噪"


def test_prepare_distillation_reuses_filtered_without_refiltering(
    base_config: AppConfig, counted_filter: list[NormalizedSession]
):
    """``filtered`` 生效的直接证据：传了就不再降噪，不传仍保持原行为。"""
    session = noisy_session()
    filtered = noise.filter_noise(session)
    counted_filter.clear()

    with pytest.raises(DistillationRequired):
        prepare_distillation(session, base_config, filtered=filtered)
    assert counted_filter == [], "传了 filtered 就不该再降噪一次"

    with pytest.raises(DistillationRequired):
        prepare_distillation(session, base_config)
    assert len(counted_filter) == 1, "不传 filtered 时必须自己降噪一次（向后兼容）"


# --- 原始会话与副作用 --------------------------------------------------------


def test_input_session_is_never_mutated(base_config: AppConfig):
    session = noisy_session()
    before = session.to_dict()

    plan = prepare_archive(session, base_config, distilled_note=supplied_note())

    assert plan.session is session
    assert session.to_dict() == before
    assert plan.cleaned is not session
    assert plan.cleaned.to_dict() != before, "降噪后的会话应当与原文不同"
    assert plan.cleaned.state_key == session.state_key


def test_planned_modes_write_nothing(base_config: AppConfig, monkeypatch: pytest.MonkeyPatch):
    session = noisy_session()
    before = snapshot(base_config.vault)

    staging_plan = prepare_archive(session, base_config, allow_side_effects=False)
    assert staging_plan.mode == "staging-planned"
    assert staging_plan.staging_required
    assert staging_plan.staging_path is None and staging_plan.schema_path is None
    assert snapshot(base_config.vault) == before
    assert not base_config.staging_dir.exists()

    monkeypatch.setenv("EDAE_API_KEY", "sk-test-only")
    with FakeProvider(ENVELOPE) as provider:
        provider_plan = prepare_archive(
            session, with_provider(base_config, provider.base_url), allow_side_effects=False
        )
    assert provider_plan.mode == "provider-planned"
    assert provider_plan.provider_planned
    assert provider_plan.note is None
    assert provider.requests == [], "dry-run 不得调用模型"
    assert snapshot(base_config.vault) == before


def test_dry_run_and_real_run_share_the_same_content_hash(base_config: AppConfig):
    session = noisy_session()
    included = supplied_note()

    planned = prepare_archive(session, base_config, distilled_note=included, allow_side_effects=False)
    before = snapshot(base_config.vault)
    dry = writer.write_note(planned.note, planned.session, base_config, dry_run=True)
    assert dry.dry_run
    assert snapshot(base_config.vault) == before, "dry-run 一个字节都不能写"

    real_plan = prepare_archive(session, base_config, distilled_note=included)
    assert writer.content_hash_of(real_plan.note, real_plan.session) == writer.content_hash_of(
        planned.note, planned.session
    )
    assert dry.content_hash == writer.content_hash_of(real_plan.note, real_plan.session)

    written = writer.write_note(real_plan.note, real_plan.session, base_config)
    assert written.content_hash == dry.content_hash
    assert written.action == dry.action


# --- staging 与落盘文本 ------------------------------------------------------


def test_staging_request_carries_pipeline_stats(base_config: AppConfig):
    plan = prepare_archive(noisy_session(), base_config)

    assert plan.mode == "staging"
    assert plan.note is None
    assert plan.staging_path is not None and plan.staging_path.exists()
    assert plan.schema_path is not None and plan.schema_path.exists()

    text = plan.staging_path.read_text(encoding="utf-8")
    assert f"- 消息: 保留 {NOISY_KEPT} / 输入 {NOISY_INPUT}" in text
    assert f"- kept_messages: {NOISY_KEPT}" in text
    assert f"- input_messages: {NOISY_INPUT}" in text
    # 提示词用的是降噪后的会话
    assert "<app-context>" not in text
    assert "abcdef1234567890" not in text


def test_written_note_uses_plan_report(base_config: AppConfig):
    session = noisy_session()
    plan = prepare_archive(session, base_config, distilled_note=supplied_note())
    assert plan.note is not None

    result = writer.write_note(plan.note, plan.session, base_config)
    text = result.path.read_text(encoding="utf-8")

    assert f"过滤消息 {NOISY_KEPT}/{NOISY_INPUT}" in text
    assert "过滤消息 6/6" not in text


def test_pending_candidate_uses_plan_report(base_config: AppConfig):
    session = noisy_session()
    plan = prepare_archive(session, base_config, distilled_note=supplied_note())
    assert plan.note is not None

    occupied = base_config.archive_dir / plan.note.project / f"{plan.note.title}.md"
    occupied.parent.mkdir(parents=True, exist_ok=True)
    occupied.write_text("手工笔记，工具不得覆盖\n", encoding="utf-8")

    result = writer.write_note(plan.note, plan.session, base_config)
    assert result.action == writer.ACTION_PENDING
    assert writer.is_pending_filename(result.path.name)

    text = result.path.read_text(encoding="utf-8")
    assert f"过滤消息 {NOISY_KEPT}/{NOISY_INPUT}" in text
    assert "过滤消息 6/6" not in text
    assert occupied.read_text(encoding="utf-8") == "手工笔记，工具不得覆盖\n"
