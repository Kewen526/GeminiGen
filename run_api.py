#!/usr/bin/env python3
"""API entrypoint — resolves stdlib/local 'platform' name conflict."""

from __future__ import annotations

import sys
from pathlib import Path

# ── Step 1: remove project root from sys.path BEFORE any imports.
# Python automatically inserts the script's directory as sys.path[0] when the
# interpreter starts.  Our local platform/ directory would shadow stdlib
# 'platform', causing pydantic-core to fail during C-extension initialisation.
_project_root = str(Path(__file__).resolve().parent)
while _project_root in sys.path:
    sys.path.remove(_project_root)

# ── Step 2: import all external packages now that stdlib platform is clean.
import os          # noqa: E402
import uvicorn     # noqa: E402
import fastapi     # noqa: E402
import pydantic    # noqa: E402

# ── Step 3: restore project root so our local platform/ package is findable.
sys.path.insert(0, _project_root)

# ── Step 4: swap out stdlib platform, import local package, restore.
_stdlib_platform = sys.modules.pop("platform", None)

from platform.main import app        # noqa: E402  — local platform/ package
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
