from __future__ import annotations

from obsidian_knowledge_distiller import __version__
from obsidian_knowledge_distiller.cli import main


def test_version(capsys):
    assert main(["--version"]) == 0
    assert "0.1.0" in capsys.readouterr().out


def test_package_version_matches_release():
    assert __version__ == "0.1.0"
