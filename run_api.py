#!/usr/bin/env python3
"""Stable API entrypoint that avoids stdlib `platform` shadowing issues."""

from __future__ import annotations

import importlib.util
import os
import sys
import sysconfig
from pathlib import Path


def _load_stdlib_platform() -> None:
    stdlib = sysconfig.get_paths().get("stdlib")
    if not stdlib:
        return
    std_platform = Path(stdlib) / "platform.py"
    if not std_platform.exists():
        return
    spec = importlib.util.spec_from_file_location("platform", std_platform)
    if not spec or not spec.loader:
        return
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.modules["platform"] = mod


def _load_app():
    project_root = Path(__file__).resolve().parent

    # Prefer compatibility layer when present.
    app_main = project_root / "app" / "main.py"
    if app_main.exists():
        spec = importlib.util.spec_from_file_location("geminigen_app_main", app_main)
        mod = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(mod)
        return mod.app

    # Fallback to platform package file-path load.
    platform_main = project_root / "platform" / "main.py"
    if platform_main.exists():
        spec = importlib.util.spec_from_file_location("geminigen_platform_main", platform_main)
        mod = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(mod)
        return mod.app

    raise RuntimeError("No API entrypoint found: app/main.py or platform/main.py")


def main() -> None:
    _load_stdlib_platform()
    import uvicorn

    app = _load_app()
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host=host, port=port, reload=False, ws="none", loop="asyncio", http="h11")


if __name__ == "__main__":
    main()

