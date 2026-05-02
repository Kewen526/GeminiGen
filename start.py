# -*- coding: utf-8 -*-
"""
Kewen AI Pipeline - 多进程启动器
====================================
在此处填写账号，每组账号启动一个独立进程（独立浏览器）。
一台机器跑两个账号就填两组，三个就填三组。

用法：
    python start.py
"""

import os
import sys
import time
import subprocess
import signal
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ============================================================
# 👇 在这里填写账号密码（有几组就填几组）
# ============================================================
ACCOUNTS = [
    {"username": "kewen789456@gmail.com",       "password": "Kewen888@"},
    {"username": "hailingchen85@gmail.com",      "password": "Mima123456"},
    {"username": "wenwenc033@gmail.com",         "password": "MIma123456!"},
    {"username": "wangjingqing359@gmail.com",    "password": "Mima123456"},
]
# ============================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MAIN_LOOP  = os.path.join(SCRIPT_DIR, "main_loop.py")


def main():
    if not ACCOUNTS:
        logger.error("❌ ACCOUNTS 为空，请至少填写一组账号")
        sys.exit(1)

    logger.info("=" * 60)
    logger.info(f"Kewen AI Pipeline 启动器 — 共 {len(ACCOUNTS)} 个账号")
    logger.info("=" * 60)

    processes = []

    for idx, account in enumerate(ACCOUNTS):
        username = account["username"]
        password = account["password"]
        logger.info(f"启动进程 #{idx}  账号: {username}")

        cmd = [
            sys.executable, MAIN_LOOP,
            "--idx",      str(idx),
            "--username", username,
            "--password", password,
        ]

        # 每个进程使用独立的日志文件（stdout重定向到 pipeline_N.log，同时也在控制台显示）
        log_path = os.path.join(SCRIPT_DIR, f"pipeline_{idx}.log")
        log_file = open(log_path, "a", encoding="utf-8")

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            universal_newlines=True,
            encoding="utf-8",
        )
        processes.append({"proc": proc, "idx": idx, "log": log_file, "username": username})
        logger.info(f"  ✅ 进程 #{idx} PID={proc.pid}  日志: pipeline_{idx}.log")

        # 两个进程之间错开5秒，避免同时抢Chrome资源
        if idx < len(ACCOUNTS) - 1:
            time.sleep(5)

    logger.info(f"全部 {len(processes)} 个进程已启动，Ctrl+C 可终止所有进程")
    logger.info("=" * 60)

    # ── 转发各子进程输出到控制台 + 独立日志文件 ──
    import threading

    def pipe_output(entry):
        proc    = entry["proc"]
        idx     = entry["idx"]
        log_f   = entry["log"]
        prefix  = f"[进程#{idx}]"
        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                print(f"{prefix} {line}", flush=True)
                log_f.write(line + "\n")
                log_f.flush()
        except Exception:
            pass
        finally:
            log_f.close()

    threads = []
    for entry in processes:
        t = threading.Thread(target=pipe_output, args=(entry,), daemon=True)
        t.start()
        threads.append(t)

    # ── 等待并监控进程 ──
    def terminate_all(signum=None, frame=None):
        logger.info("\n收到中断信号，正在终止所有子进程...")
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
        logger.info("所有子进程已终止")
        sys.exit(0)

    signal.signal(signal.SIGINT,  terminate_all)
    signal.signal(signal.SIGTERM, terminate_all)

    try:
        while True:
            time.sleep(5)
            for entry in processes:
                ret = entry["proc"].poll()
                if ret is not None:
                    logger.warning(
                        f"⚠ 进程 #{entry['idx']} ({entry['username']}) "
                        f"意外退出，退出码={ret}，10秒后重启..."
                    )
                    time.sleep(10)
                    # 重启该进程
                    idx      = entry["idx"]
                    username = entry["username"]
                    password = next(
                        a["password"] for a in ACCOUNTS if a["username"] == username
                    )
                    cmd = [
                        sys.executable, MAIN_LOOP,
                        "--idx",      str(idx),
                        "--username", username,
                        "--password", password,
                    ]
                    log_path = os.path.join(SCRIPT_DIR, f"pipeline_{idx}.log")
                    log_file = open(log_path, "a", encoding="utf-8")
                    proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        bufsize=1,
                        universal_newlines=True,
                        encoding="utf-8",
                    )
                    entry["proc"] = proc
                    entry["log"]  = log_file
                    logger.info(f"  ✅ 进程 #{idx} 已重启，PID={proc.pid}")
                    t = threading.Thread(target=pipe_output, args=(entry,), daemon=True)
                    t.start()
    except KeyboardInterrupt:
        terminate_all()


if __name__ == "__main__":
    main()