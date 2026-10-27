"""Task 11 补充：有 provider 的完整链路、--include-stale、uninstall 安全性。

假模型服务只监听 127.0.0.1 随机端口，不访问任何真实服务。
"""

from __future__ import annotations

import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from experience_data_asset_engine.cli import EXIT_FAILURE, EXIT_OK, main
from experience_data_asset_engine.config import STATE_DIRNAME

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MANUAL_MD = FIXTURES / "manual-session.md"
NOISY_JSON = FIXTURES / "noisy-session.json"
DISTILLED = FIXTURES / "distilled-e2e.json"
ARCHIVE_REL = "04-对话存档"


def run(capsys: pytest.CaptureFixture, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def archived_notes(vault: Path) -> list[Path]:
    return sorted((vault / ARCHIVE_REL).rglob("*.md"))


@pytest.fixture
def vault(sample_vault: Path, capsys: pytest.CaptureFixture) -> Path:
    assert run(capsys, "init", "--vault", str(sample_vault))[0] == EXIT_OK
    return sample_vault


class FakeProvider:
    """最小假模型服务：记录请求体，返回固定 envelope。"""

    def __init__(self, payload: dict):
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                outer.requests.append({"raw": raw, "headers": dict(self.headers)})
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
                    pass

            def log_message(self, *args) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )

    def __enter__(self) -> "FakeProvider":
        self._thread.start()
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}/v1"
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()


def envelope(note: dict) -> dict:
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(note, ensure_ascii=False)},
            }
        ]
    }


def use_provider(monkeypatch: pytest.MonkeyPatch, base_url: str, api_key: str = "sk-test-only") -> None:
    monkeypatch.setenv("EDAE_PROVIDER", "openai-compatible")
    monkeypatch.setenv("EDAE_MODEL", "demo-model")
    monkeypatch.setenv("EDAE_BASE_URL", base_url)
    if api_key:
        monkeypatch.setenv("EDAE_API_KEY", api_key)
    else:
        monkeypatch.delenv("EDAE_API_KEY", raising=False)


# --- 有 provider：降噪 → 蒸馏 → 校验 → 写入 ------------------------------------


def test_archive_with_provider_runs_full_pipeline(vault: Path, capsys, monkeypatch):
    note = json.loads(DISTILLED.read_text(encoding="utf-8"))
    with FakeProvider(envelope(note)) as provider:
        use_provider(monkeypatch, provider.base_url)
        code, out, err = run(capsys, "archive", "--input", str(NOISY_JSON), "--vault", str(vault))

    assert code == EXIT_OK, err
    assert "action: created" in out
    assert "来源: --distilled" not in out, "有 provider 时不该走 --distilled 分支"
    assert "noise: 保留 3/10" in out, "统计必须来自管道，而不是模型自报的 6/6"
    assert len(archived_notes(vault)) == 1

    raw = provider.requests[0]["raw"].decode("utf-8")
    assert "abcdef1234567890" not in raw, "会话密钥不得进入提示词"
    assert "[已脱敏]" in raw
    assert provider.requests[0]["headers"]["Authorization"] == "Bearer sk-test-only"


def test_archive_with_provider_but_no_key_fails(vault: Path, capsys, monkeypatch):
    use_provider(monkeypatch, "http://127.0.0.1:1/v1", api_key="")
    code, _, err = run(capsys, "archive", "--input", str(MANUAL_MD), "--vault", str(vault))

    assert code == EXIT_FAILURE
    assert "EDAE_API_KEY" in err
    assert archived_notes(vault) == []


def test_archive_with_provider_bad_response_writes_nothing(vault: Path, capsys, monkeypatch):
    with FakeProvider({"choices": []}) as provider:
        use_provider(monkeypatch, provider.base_url)
        code, _, err = run(capsys, "archive", "--input", str(MANUAL_MD), "--vault", str(vault))

    assert code == EXIT_FAILURE
    assert "choices" in err
    assert archived_notes(vault) == []


# --- --include-stale ---------------------------------------------------------


def test_include_stale_changes_vault_summary(vault: Path, capsys):
    base = (
        "archive",
        "--input",
        str(MANUAL_MD),
        "--distilled",
        str(DISTILLED),
        "--vault",
        str(vault),
    )
    assert run(capsys, *base)[0] == EXIT_OK
    note = archived_notes(vault)[0]
    assert (
        run(capsys, "recheck", "--note", str(note), "--status", "已过时", "--vault", str(vault))[0]
        == EXIT_OK
    )

    _, out, _ = run(capsys, *base)
    assert "检索可见 0 篇（不含过期）" in out

    _, out_stale, _ = run(capsys, *base, "--include-stale")
    assert "检索可见 1 篇（含过期）" in out_stale


# --- uninstall 的安全性 -------------------------------------------------------


def test_uninstall_refuses_symlinked_state_dir(sample_vault: Path, tmp_path: Path, capsys):
    assert run(capsys, "init", "--vault", str(sample_vault))[0] == EXIT_OK
    state_dir = sample_vault / STATE_DIRNAME
    shutil.rmtree(state_dir)
    outside = tmp_path / "outside-state"
    outside.mkdir()
    try:
        state_dir.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - 依赖宿主权限
        pytest.skip("当前环境不支持创建目录符号链接")

    code, _, err = run(capsys, "uninstall", "--vault", str(sample_vault), "--yes")

    assert code == EXIT_FAILURE
    assert "符号链接" in err
    assert outside.is_dir()


def test_uninstall_refuses_state_path_that_is_a_file(sample_vault: Path, capsys):
    assert run(capsys, "init", "--vault", str(sample_vault))[0] == EXIT_OK
    state_dir = sample_vault / STATE_DIRNAME
    shutil.rmtree(state_dir)
    state_dir.write_text("not a dir", encoding="utf-8")

    code, _, err = run(capsys, "uninstall", "--vault", str(sample_vault), "--yes")

    assert code == EXIT_FAILURE
    assert "不是目录" in err
    assert state_dir.read_text(encoding="utf-8") == "not a dir"
