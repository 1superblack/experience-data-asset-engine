from __future__ import annotations

import dataclasses
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from experience_data_asset_engine import distill as distill_module
from experience_data_asset_engine.adapters.normalized_json import load_normalized_json
from experience_data_asset_engine.config import (
    CONFIG_FILENAME,
    DEFAULT_ARCHIVE_REL,
    STATE_DIRNAME,
    AppConfig,
    ConfigError,
)
from experience_data_asset_engine.distill import (
    DistillationRequired,
    note_json_schema,
    prepare_distillation,
    validate_distilled_note,
)
from experience_data_asset_engine.errors import DistillationError, SchemaError
from experience_data_asset_engine.models import NormalizedSession
from experience_data_asset_engine.noise import REDACTED

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SESSION = FIXTURES / "session.json"
ENVELOPE = json.loads((FIXTURES / "distilled.json").read_text(encoding="utf-8"))
SECRET = "sk-abcdefghijklmnop1234"


# --- 工具 -------------------------------------------------------------------


def note_payload() -> dict:
    return json.loads(ENVELOPE["choices"][0]["message"]["content"])


def envelope_with(note: dict, finish_reason: str = "stop") -> dict:
    return {
        "id": "chatcmpl-synthetic",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": json.dumps(note, ensure_ascii=False)},
            }
        ],
    }


def make_session(messages: list[dict], session_id: str = "s-distill") -> NormalizedSession:
    return NormalizedSession.from_dict(
        {
            "schema_version": 1,
            "source": "manual",
            "session_id": session_id,
            "title": "蒸馏测试",
            "created_at": "2026-10-27T09:00:00+08:00",
            "updated_at": "2026-10-27T09:10:00+08:00",
            "messages": messages,
            "metadata": {},
        }
    )


def secret_session() -> NormalizedSession:
    return make_session(
        [
            {"role": "user", "text": f"配置里 api_key={SECRET}，先记着，别写进笔记。", "kind": "message"},
            {"role": "assistant", "text": "明白，我会替换掉再继续。", "kind": "message"},
        ]
    )


class FakeProvider:
    """本地假模型服务：只监听 127.0.0.1 随机端口，记录请求体供断言。"""

    def __init__(self, responder):
        self._responder = responder
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.requests: list[dict] = []

    def __enter__(self) -> "FakeProvider":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    body = {}
                record = {
                    "path": self.path,
                    "headers": {key.lower(): value for key, value in self.headers.items()},
                    "body": body,
                    "raw": raw,
                }
                outer.requests.append(record)
                status, payload, delay = outer._responder(record)
                if delay:
                    time.sleep(delay)
                data = (
                    payload
                    if isinstance(payload, bytes)
                    else json.dumps(payload, ensure_ascii=False).encode("utf-8")
                )
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):  # pragma: no cover - 超时用例
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


def with_provider(config: AppConfig, base_url: str) -> AppConfig:
    return dataclasses.replace(
        config, provider="openai-compatible", model="demo-model", base_url=base_url
    )


def ok_responder(record):
    return 200, ENVELOPE, 0.0


# --- 无 provider：只写 staging -------------------------------------------------


def test_no_provider_creates_staging_without_raw_note(base_config):
    session = load_normalized_json(SESSION)

    with pytest.raises(DistillationRequired) as err:
        prepare_distillation(session, base_config)

    request_path = err.value.staging_path
    assert request_path.exists()
    assert "prompt" in request_path.read_text(encoding="utf-8")
    assert err.value.schema_path is not None and err.value.schema_path.exists()
    assert list(base_config.archive_dir.iterdir()) == []


def test_staging_filename_uses_state_key(base_config):
    session = load_normalized_json(SESSION)

    with pytest.raises(DistillationRequired) as err:
        prepare_distillation(session, base_config)

    assert err.value.staging_path.name == f"{session.state_key}.request.md"
    assert session.session_id not in err.value.staging_path.name
    assert err.value.schema_path is not None
    assert err.value.schema_path.name == f"{session.state_key}.schema.json"


def test_staging_never_writes_markdown_outside_state_dir(base_config):
    session = load_normalized_json(SESSION)

    with pytest.raises(DistillationRequired):
        prepare_distillation(session, base_config)

    outside_state = [
        path for path in base_config.vault.rglob("*") if STATE_DIRNAME not in path.parts
    ]
    assert [path for path in outside_state if path.is_file()] == []


def test_staging_flags_distillation_required_for_cli(base_config):
    with pytest.raises(DistillationRequired) as err:
        prepare_distillation(load_normalized_json(SESSION), base_config)
    assert err.value.exit_code == 3


def test_staging_contains_redacted_text_not_secret(base_config):
    with pytest.raises(DistillationRequired) as err:
        prepare_distillation(secret_session(), base_config)

    text = err.value.staging_path.read_text(encoding="utf-8")
    assert SECRET not in text
    assert REDACTED in text


def titled_secret_session() -> NormalizedSession:
    """标题里带凭据：标题同样会进 staging 与模型提示词，必须一起脱敏。"""
    return NormalizedSession.from_dict(
        {
            "schema_version": 1,
            "source": "manual",
            "session_id": "s-title-secret",
            "title": f"排查 api_key={SECRET} 泄漏",
            "created_at": "2026-10-27T09:00:00+08:00",
            "updated_at": "2026-10-27T09:10:00+08:00",
            "messages": [{"role": "user", "text": "标题里的密钥也要挡住。", "kind": "message"}],
            "metadata": {},
        }
    )


def test_staging_title_is_redacted(base_config):
    with pytest.raises(DistillationRequired) as err:
        prepare_distillation(titled_secret_session(), base_config)

    text = err.value.staging_path.read_text(encoding="utf-8")
    assert SECRET not in text
    title_line = next(line for line in text.splitlines() if line.startswith("- 标题: "))
    assert REDACTED in title_line


def test_staging_prints_the_validated_source(base_config):
    with pytest.raises(DistillationRequired) as err:
        prepare_distillation(load_normalized_json(SESSION), base_config)

    text = err.value.staging_path.read_text(encoding="utf-8")
    assert "- 来源: codex" in text


def test_provider_prompt_redacts_title_secret(base_config, monkeypatch):
    monkeypatch.setenv("EDAE_API_KEY", "sk-test-only")
    with FakeProvider(ok_responder) as provider:
        prepare_distillation(
            titled_secret_session(), with_provider(base_config, provider.base_url)
        )

    assert provider.requests, "假模型服务没有收到请求"
    body = json.dumps(provider.requests[0]["body"], ensure_ascii=False)
    assert SECRET not in body
    assert REDACTED in body


def test_staging_schema_lists_noise_counters(base_config):
    with pytest.raises(DistillationRequired) as err:
        prepare_distillation(load_normalized_json(SESSION), base_config)

    assert err.value.schema_path is not None
    schema = json.loads(err.value.schema_path.read_text(encoding="utf-8"))
    assert schema["properties"]["noise_report"]["required"] == [
        "input_messages",
        "kept_messages",
        "system_noise",
        "tool_noise",
        "duplicate_topics",
        "dead_ends",
        "redactions",
    ]
    assert schema["properties"]["decisions"]["minItems"] == 1


def test_staging_rejects_symlinked_staging_dir(base_config, tmp_path):
    outside = tmp_path / "outside-staging"
    outside.mkdir()
    staging_dir = base_config.staging_dir
    try:
        staging_dir.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - 依赖宿主权限
        pytest.skip("当前环境不支持创建目录符号链接")
    before = sorted(path.name for path in outside.iterdir())

    with pytest.raises(ConfigError, match="outside vault"):
        prepare_distillation(load_normalized_json(SESSION), base_config)

    assert sorted(path.name for path in outside.iterdir()) == before


# --- 有 provider：凭据与配置 ---------------------------------------------------


def test_missing_api_key_raises_config_error(base_config, monkeypatch):
    config = with_provider(base_config, "http://127.0.0.1:9/v1")
    monkeypatch.delenv("EDAE_API_KEY", raising=False)

    with pytest.raises(ConfigError, match="EDAE_API_KEY"):
        prepare_distillation(load_normalized_json(SESSION), config)
    assert not base_config.staging_dir.exists()


def test_missing_base_url_raises_config_error(base_config, monkeypatch):
    monkeypatch.setenv("EDAE_API_KEY", "sk-test-only")
    config = dataclasses.replace(
        base_config, provider="openai-compatible", model="demo-model", base_url=None
    )

    with pytest.raises(ConfigError, match="base_url"):
        prepare_distillation(load_normalized_json(SESSION), config)


# --- 有 provider：成功路径 -----------------------------------------------------


def test_provider_success_returns_validated_note(base_config, monkeypatch):
    monkeypatch.setenv("EDAE_API_KEY", "sk-test-only")
    with FakeProvider(ok_responder) as provider:
        note = prepare_distillation(load_normalized_json(SESSION), with_provider(base_config, provider.base_url))

    assert note.title == "降噪与蒸馏的边界划分"
    assert note.decisions[0].rejected
    assert note.status == "有效"
    assert note.review_after == "2027-01-25"
    assert provider.requests, "假服务应收到一次请求"
    assert provider.requests[0]["path"].endswith("/chat/completions")


def test_model_claimed_noise_report_is_overridden(base_config, monkeypatch):
    monkeypatch.setenv("EDAE_API_KEY", "sk-test-only")
    bogus = note_payload()
    bogus["noise_report"] = {
        "input_messages": 999,
        "kept_messages": 999,
        "system_noise": 0,
        "tool_noise": 0,
        "duplicate_topics": 0,
        "dead_ends": 0,
        "redactions": 0,
    }

    with FakeProvider(lambda record: (200, envelope_with(bogus), 0.0)) as provider:
        note = prepare_distillation(load_normalized_json(SESSION), with_provider(base_config, provider.base_url))

    assert note.noise_report.input_messages == 2
    assert note.noise_report.kept_messages == 2
    assert note.noise_report.redactions == 0


def test_prompt_lists_required_fields_and_noise_numbers(base_config, monkeypatch):
    monkeypatch.setenv("EDAE_API_KEY", "sk-test-only")
    with FakeProvider(ok_responder) as provider:
        prepare_distillation(load_normalized_json(SESSION), with_provider(base_config, provider.base_url))

    body = provider.requests[0]["body"]
    joined = json.dumps(body, ensure_ascii=False)
    for field in (
        "summary",
        "background",
        "diagnosis",
        "decisions",
        "evidence",
        "reusable",
        "risks",
        "todo",
        "noise_report",
        "content_volatility",
        "status",
        "review_after",
        "rejected",
    ):
        assert field in joined, field
    assert "input_messages" in joined


def test_request_body_contains_no_secret(base_config, monkeypatch):
    monkeypatch.setenv("EDAE_API_KEY", "sk-test-only")
    with FakeProvider(ok_responder) as provider:
        prepare_distillation(secret_session(), with_provider(base_config, provider.base_url))

    raw = provider.requests[0]["raw"].decode("utf-8")
    assert SECRET not in raw
    assert REDACTED in raw


def test_authorization_header_uses_bearer_without_logging_key(base_config, monkeypatch, caplog):
    monkeypatch.setenv("EDAE_API_KEY", "sk-test-only")
    with FakeProvider(ok_responder) as provider:
        prepare_distillation(load_normalized_json(SESSION), with_provider(base_config, provider.base_url))

    headers = provider.requests[0]["headers"]
    assert headers["authorization"] == "Bearer sk-test-only"
    assert caplog.records == []


def test_fenced_json_content_is_accepted(base_config, monkeypatch):
    monkeypatch.setenv("EDAE_API_KEY", "sk-test-only")
    fenced = "```json\n" + json.dumps(note_payload(), ensure_ascii=False) + "\n```"
    envelope = {
        "choices": [{"finish_reason": "stop", "message": {"content": fenced}}],
    }

    with FakeProvider(lambda record: (200, envelope, 0.0)) as provider:
        note = prepare_distillation(load_normalized_json(SESSION), with_provider(base_config, provider.base_url))

    assert note.title == "降噪与蒸馏的边界划分"


# --- 有 provider：失败关闭 -----------------------------------------------------


def test_provider_http_error_fails_closed(base_config, monkeypatch):
    monkeypatch.setenv("EDAE_API_KEY", "sk-test-only")
    with FakeProvider(lambda record: (500, {"error": "internal"}, 0.0)) as provider:
        with pytest.raises(DistillationError, match="HTTP 500"):
            prepare_distillation(
                load_normalized_json(SESSION), with_provider(base_config, provider.base_url)
            )


def test_provider_timeout_fails_closed(base_config, monkeypatch):
    monkeypatch.setenv("EDAE_API_KEY", "sk-test-only")
    monkeypatch.setattr(distill_module, "REQUEST_TIMEOUT_SECONDS", 0.2)
    with FakeProvider(lambda record: (200, ENVELOPE, 0.6)) as provider:
        with pytest.raises(DistillationError, match="超时"):
            prepare_distillation(
                load_normalized_json(SESSION), with_provider(base_config, provider.base_url)
            )


def test_provider_non_json_envelope_fails_closed(base_config, monkeypatch):
    monkeypatch.setenv("EDAE_API_KEY", "sk-test-only")
    with FakeProvider(lambda record: (200, b"not json at all", 0.0)) as provider:
        with pytest.raises(DistillationError, match="不是合法 JSON"):
            prepare_distillation(
                load_normalized_json(SESSION), with_provider(base_config, provider.base_url)
            )


def test_provider_content_not_json_fails_closed(base_config, monkeypatch):
    monkeypatch.setenv("EDAE_API_KEY", "sk-test-only")
    envelope = {"choices": [{"finish_reason": "stop", "message": {"content": "这是一段总结，不是 JSON"}}]}
    with FakeProvider(lambda record: (200, envelope, 0.0)) as provider:
        with pytest.raises(DistillationError, match="不是合法 JSON"):
            prepare_distillation(
                load_normalized_json(SESSION), with_provider(base_config, provider.base_url)
            )


def test_provider_truncated_response_fails_closed(base_config, monkeypatch):
    monkeypatch.setenv("EDAE_API_KEY", "sk-test-only")
    with FakeProvider(lambda record: (200, envelope_with(note_payload(), "length"), 0.0)) as provider:
        with pytest.raises(DistillationError, match="截断"):
            prepare_distillation(
                load_normalized_json(SESSION), with_provider(base_config, provider.base_url)
            )


def test_provider_empty_choices_fails_closed(base_config, monkeypatch):
    monkeypatch.setenv("EDAE_API_KEY", "sk-test-only")
    with FakeProvider(lambda record: (200, {"choices": []}, 0.0)) as provider:
        with pytest.raises(DistillationError, match="choices"):
            prepare_distillation(
                load_normalized_json(SESSION), with_provider(base_config, provider.base_url)
            )


def test_provider_summary_only_fails_closed(base_config, monkeypatch):
    monkeypatch.setenv("EDAE_API_KEY", "sk-test-only")
    summary_only = {"title": "x", "summary": "只有摘要"}
    with FakeProvider(lambda record: (200, envelope_with(summary_only), 0.0)) as provider:
        with pytest.raises(SchemaError, match="decisions"):
            prepare_distillation(
                load_normalized_json(SESSION), with_provider(base_config, provider.base_url)
            )


def test_provider_all_rejected_empty_fails_closed(base_config, monkeypatch):
    monkeypatch.setenv("EDAE_API_KEY", "sk-test-only")
    payload = note_payload()
    payload["decisions"] = [
        {"decision": "方案 A", "reason": "理由 A", "rejected": []},
        {"decision": "方案 B", "reason": "理由 B"},
    ]
    with FakeProvider(lambda record: (200, envelope_with(payload), 0.0)) as provider:
        with pytest.raises(SchemaError, match="rejected"):
            prepare_distillation(
                load_normalized_json(SESSION), with_provider(base_config, provider.base_url)
            )


def test_provider_bad_status_fails_closed(base_config, monkeypatch):
    monkeypatch.setenv("EDAE_API_KEY", "sk-test-only")
    payload = note_payload()
    payload["status"] = "大概有效"
    with FakeProvider(lambda record: (200, envelope_with(payload), 0.0)) as provider:
        with pytest.raises(SchemaError, match="status"):
            prepare_distillation(
                load_normalized_json(SESSION), with_provider(base_config, provider.base_url)
            )


def test_provider_connection_refused_fails_closed(base_config, monkeypatch):
    monkeypatch.setenv("EDAE_API_KEY", "sk-test-only")
    config = with_provider(base_config, "http://127.0.0.1:1/v1")

    with pytest.raises(DistillationError, match="网络或超时"):
        prepare_distillation(load_normalized_json(SESSION), config)


def test_provider_oversized_response_fails_closed(base_config, monkeypatch):
    monkeypatch.setenv("EDAE_API_KEY", "sk-test-only")
    monkeypatch.setattr(distill_module, "MAX_RESPONSE_BYTES", 64)
    with FakeProvider(ok_responder) as provider:
        with pytest.raises(DistillationError, match="大小上限"):
            prepare_distillation(
                load_normalized_json(SESSION), with_provider(base_config, provider.base_url)
            )


def test_provider_empty_content_fails_closed(base_config, monkeypatch):
    monkeypatch.setenv("EDAE_API_KEY", "sk-test-only")
    envelope = {"choices": [{"finish_reason": "stop", "message": {"content": "   "}}]}
    with FakeProvider(lambda record: (200, envelope, 0.0)) as provider:
        with pytest.raises(DistillationError, match="内容为空"):
            prepare_distillation(
                load_normalized_json(SESSION), with_provider(base_config, provider.base_url)
            )


def test_provider_envelope_not_object_fails_closed(base_config, monkeypatch):
    monkeypatch.setenv("EDAE_API_KEY", "sk-test-only")
    with FakeProvider(lambda record: (200, [1, 2, 3], 0.0)) as provider:
        with pytest.raises(DistillationError, match="顶层不是对象"):
            prepare_distillation(
                load_normalized_json(SESSION), with_provider(base_config, provider.base_url)
            )


def test_provider_choice_not_object_fails_closed(base_config, monkeypatch):
    monkeypatch.setenv("EDAE_API_KEY", "sk-test-only")
    with FakeProvider(lambda record: (200, {"choices": ["nope"]}, 0.0)) as provider:
        with pytest.raises(DistillationError, match="choice"):
            prepare_distillation(
                load_normalized_json(SESSION), with_provider(base_config, provider.base_url)
            )


def test_provider_message_not_object_fails_closed(base_config, monkeypatch):
    monkeypatch.setenv("EDAE_API_KEY", "sk-test-only")
    envelope = {"choices": [{"finish_reason": "stop", "message": "not-a-dict"}]}
    with FakeProvider(lambda record: (200, envelope, 0.0)) as provider:
        with pytest.raises(DistillationError, match="message"):
            prepare_distillation(
                load_normalized_json(SESSION), with_provider(base_config, provider.base_url)
            )


def test_provider_content_is_json_array_fails_closed(base_config, monkeypatch):
    monkeypatch.setenv("EDAE_API_KEY", "sk-test-only")
    envelope = {"choices": [{"finish_reason": "stop", "message": {"content": "[1, 2, 3]"}}]}
    with FakeProvider(lambda record: (200, envelope, 0.0)) as provider:
        with pytest.raises(SchemaError, match="JSON 对象"):
            prepare_distillation(
                load_normalized_json(SESSION), with_provider(base_config, provider.base_url)
            )


def test_long_conversation_is_truncated_in_prompt(base_config, monkeypatch):
    monkeypatch.setenv("EDAE_API_KEY", "sk-test-only")
    # 每段用不同汉字填充，避免被近似重复规则合并；25 段 × 2005 字 > 40000 上限
    long_session = make_session(
        [
            {"role": "user", "text": f"第 {index} 段：" + chr(0x4E00 + index) * 2000, "kind": "message"}
            for index in range(25)
        ]
    )
    with FakeProvider(ok_responder) as provider:
        prepare_distillation(long_session, with_provider(base_config, provider.base_url))

    user_content = provider.requests[0]["body"]["messages"][1]["content"]
    assert "省略" in user_content
    assert len(user_content) < distill_module.MAX_PROMPT_CONVERSATION_CHARS + 2000


def test_error_message_does_not_leak_secret(base_config, monkeypatch):
    monkeypatch.setenv("EDAE_API_KEY", "sk-test-only")
    with FakeProvider(lambda record: (500, {"error": "boom"}, 0.0)) as provider:
        with pytest.raises(DistillationError) as err:
            prepare_distillation(secret_session(), with_provider(base_config, provider.base_url))
    assert SECRET not in str(err.value)


# --- 协议校验 -----------------------------------------------------------------


def test_validate_distilled_note_accepts_fixture():
    note = validate_distilled_note(note_payload())
    assert note.project == "数据资产引擎"
    assert note.decisions[0].rejected


def test_validate_distilled_note_rejects_summary_only():
    with pytest.raises(SchemaError, match="decisions"):
        validate_distilled_note({"title": "x", "summary": "only a summary"})


def test_schema_documents_rejected_requirement():
    schema = note_json_schema()
    assert "rejected" in schema["properties"]["decisions"]["description"]
    assert schema["required"].count("review_after") == 1


def test_no_provider_does_not_create_config_file(base_config):
    with pytest.raises(DistillationRequired):
        prepare_distillation(load_normalized_json(SESSION), base_config)
    assert not (base_config.state_dir / CONFIG_FILENAME).exists()
