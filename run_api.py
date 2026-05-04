#!/usr/bin/env python3
"""API entrypoint — resolves stdlib/local 'platform' name conflict."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parent

# ── Step 1: Pre-load all external packages while stdlib platform is intact.
# pydantic-core calls platform.system() during initialisation; importing
# these first ensures they complete successfully before we swap sys.modules.
import uvicorn          # noqa: E402
import fastapi          # noqa: E402
import pydantic         # noqa: E402

# ── Step 2: Add project root so our local platform/ package is importable.
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

# ── Step 3: Temporarily remove stdlib platform, import local package, restore.
# fastapi/pydantic are already cached in sys.modules so they won't re-import
# platform during this window.
_stdlib_platform = sys.modules.pop("platform", None)

from platform.main import app        # noqa: E402 — local platform/ package
from platform.config import HOST, PORT  # noqa: E402

if _stdlib_platform is not None:
    sys.modules["platform"] = _stdlib_platform   # restore for uvicorn logging


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
