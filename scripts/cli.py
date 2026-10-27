# -*- coding: utf-8 -*-
"""兼容入口：在仓库根目录直接运行，无需先安装包。

    python scripts/cli.py doctor --vault /path/to/vault
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from experience_data_asset_engine.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
