#!/usr/bin/env python3
"""API entrypoint — resolves stdlib/local 'platform' name conflict."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parent

# ── Step 1: import uvicorn while stdlib platform is still intact ──────────
import uvicorn  # noqa: E402  (needs stdlib platform for version logging)

# ── Step 2: ensure project root is on sys.path ────────────────────────────
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

# ── Step 3: swap stdlib platform out, import local package, restore ───────
_stdlib_platform = sys.modules.pop("platform", None)

from platform.main import app       # noqa: E402  — local platform/ package
from platform.config import HOST, PORT  # noqa: E402

if _stdlib_platform is not None:
    sys.modules["platform"] = _stdlib_platform  # restore for uvicorn logging


def main() -> None:
    host = os.getenv("HOST", HOST)
    port = int(os.getenv("PORT", str(PORT)))
    uvicorn.run(
        app,
        host=host,
        port=port,
        reload=False,
        ws="none",
        loop="asyncio",
        http="h11",
    )


if __name__ == "__main__":
    main()
