#!/usr/bin/env python3
"""Stable API entrypoint that avoids stdlib `platform` shadowing issues."""

from __future__ import annotations

import importlib.util
import os
import sys
import sysconfig
import types
from pathlib import Path


def _load_stdlib_platform() -> None:
    """Install a hybrid 'platform' module into sys.modules.

    The local platform/ package shadows the stdlib platform module, which
    breaks third-party libs (pydantic, cryptography, etc.) that call
    platform.system() / platform.python_version() at import time.

    The fix: build a hybrid module that exposes every stdlib platform
    attribute while also advertising the local platform/ directory as its
    __path__.  This lets Python resolve both `import platform; platform.system()`
    (stdlib) and `from platform.main import app` (local submodule) correctly.
    """
    stdlib = sysconfig.get_paths().get("stdlib")
    if not stdlib:
        return
    std_platform = Path(stdlib) / "platform.py"
    if not std_platform.exists():
        return
    spec = importlib.util.spec_from_file_location("platform", std_platform)
    if not spec or not spec.loader:
        return
    stdlib_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stdlib_mod)

    project_root = Path(__file__).resolve().parent
    local_platform = project_root / "platform"

    hybrid = types.ModuleType("platform")
    hybrid.__dict__.update(stdlib_mod.__dict__)
    if local_platform.is_dir():
        hybrid.__path__ = [str(local_platform)]
        hybrid.__package__ = "platform"
    sys.modules["platform"] = hybrid


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

