# -*- coding: utf-8 -*-
"""
GeminiGen Worker 多账号启动器
==============================
从 .env 读取账号配置，为每个账号启动独立的 worker_standalone.py 进程。

.env 配置示例（多账号）:
    GEMINIGEN_ACCOUNTS=account1@gmail.com:password1,account2@gmail.com:password2

.env 配置示例（单账号）:
    GEMINIGEN_USERNAME=account@gmail.com
    GEMINIGEN_PASSWORD=yourpassword

用法:
    python start.py
"""

from __future__ import annotations

import os
import sys
import time
import signal
import logging
import pathlib
import subprocess
import threading

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

SCRIPT_DIR    = pathlib.Path(__file__).parent
WORKER_SCRIPT = str(SCRIPT_DIR / "worker_standalone.py")

# ── 加载 .env ──────────────────────────────────────────────────
_env_path = SCRIPT_DIR / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())


def _load_accounts() -> list[dict]:
    accounts_str = os.environ.get("GEMINIGEN_ACCOUNTS", "").strip()
    if accounts_str:
        accounts = []
        for entry in accounts_str.split(","):
            entry = entry.strip()
            if ":" in entry:
                u, _, p = entry.partition(":")
                accounts.append({"username": u.strip(), "password": p.strip()})
        return accounts
    # 单账号降级
    u = os.environ.get("GEMINIGEN_USERNAME", "").strip()
    p = os.environ.get("GEMINIGEN_PASSWORD", "").strip()
    if u and p:
        return [{"username": u, "password": p}]
    return []


def _start_process(idx: int, account: dict) -> subprocess.Popen:
    cmd = [
        sys.executable, WORKER_SCRIPT,
        "--username", account["username"],
        "--password", account["password"],
        "--instance", str(idx),
    ]
    log_path = SCRIPT_DIR / f"worker_{idx}.log"
    log_file = open(log_path, "a", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        universal_newlines=True,
        encoding="utf-8",
        cwd=str(SCRIPT_DIR),
    )
    logger.info(f"进程 #{idx} 已启动  账号: {account['username']}  PID={proc.pid}  日志: worker_{idx}.log")

    def _pipe(proc: subprocess.Popen, log_file, prefix: str):
        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                print(f"{prefix} {line}", flush=True)
                log_file.write(line + "\n")
                log_file.flush()
        except Exception:
            pass
        finally:
            log_file.close()

    threading.Thread(
        target=_pipe,
        args=(proc, log_file, f"[#{idx}]"),
        daemon=True,
    ).start()
    return proc


def main():
    accounts = _load_accounts()
    if not accounts:
        logger.error("未找到账号配置！请在 .env 中设置：")
        logger.error("  多账号: GEMINIGEN_ACCOUNTS=email1:pass1,email2:pass2")
        logger.error("  单账号: GEMINIGEN_USERNAME=email  GEMINIGEN_PASSWORD=pass")
        input("\n按 Enter 退出...")
        sys.exit(1)

    logger.info("=" * 60)
    logger.info(f"GeminiGen Worker 启动器 — 共 {len(accounts)} 个账号")
    logger.info("=" * 60)

    processes: list[dict] = []
    for idx, account in enumerate(accounts):
        proc = _start_process(idx, account)
        processes.append({"proc": proc, "idx": idx, "account": account})
        if idx < len(accounts) - 1:
            time.sleep(5)  # 错开启动，避免同时争抢浏览器资源

    logger.info(f"全部 {len(processes)} 个进程已启动，按 Ctrl+C 终止")

    def _terminate_all(signum=None, frame=None):
        logger.info("收到停止信号，正在终止所有子进程...")
        for entry in processes:
            try:
                entry["proc"].terminate()
            except Exception:
                pass
        time.sleep(3)
        for entry in processes:
            try:
                entry["proc"].kill()
            except Exception:
                pass
        logger.info("全部已终止")
        sys.exit(0)

    signal.signal(signal.SIGINT,  _terminate_all)
    signal.signal(signal.SIGTERM, _terminate_all)

    try:
        while True:
            time.sleep(5)
            for entry in processes:
                if entry["proc"].poll() is not None:
                    logger.warning(
                        f"进程 #{entry['idx']} ({entry['account']['username']}) "
                        f"意外退出，10秒后重启..."
                    )
                    time.sleep(10)
                    entry["proc"] = _start_process(entry["idx"], entry["account"])
    except KeyboardInterrupt:
        _terminate_all()


if __name__ == "__main__":
    main()
