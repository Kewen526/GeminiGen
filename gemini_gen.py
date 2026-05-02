# -*- coding: utf-8 -*-
"""
GeminiGen.ai 自动化模块（Playwright版 + TokenManager 线程隔离）
================================================================
登录核心原理：
1. 在页面加载前注入 init_script，劫持 window.turnstile.render()
2. 捕获 Vue.js 注册的 Turnstile 成功回调函数
3. 用 CapSolver 解码真实 token
4. 直接调用捕获的回调，Vue 认为验证通过，按钮 enable
5. 填写邮箱密码，点击 Continue，登录请求正常发出

Token 刷新机制（fire-and-forget 模式）：
- Worker 发现 token 失效 → 立即通知 TokenManager（非阻塞），自己 sleep 后重试
- TokenManager 在后台静默完成 reload/登录，完成后 _token_ready 置位
- Worker 重试时调普通 get_token() → 若刷新还没完就等（最长 300s）
- 心跳每 60s 检测一次，token 剩余不足 60s 时提前主动刷新
- 彻底消除：greenlet 线程切换报错 / 60s 等待超时 / 重复读坏 token
"""

import hashlib
import json
import mimetypes
import os
import re
import random
import time
import logging
import threading
import traceback
from urllib.parse import parse_qs, urlencode

import requests as req_lib
from requests.exceptions import ConnectionError as ReqConnectionError, Timeout

from playwright.sync_api import sync_playwright, Page, BrowserContext, Playwright

logger = logging.getLogger(__name__)

# ============================================================
# 账号配置
# ============================================================
USERNAME      = ""
PASSWORD      = ""
_instance_idx = 0

CAPSOLVER_API_KEY = "CAP-D59F9731525D107C8073446F9B4D61D09DE2E5351DAFA3BD92E37FC3FE77284B"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

APP_URL   = "https://geminigen.ai/app/imagen"
LOGIN_URL = "https://geminigen.ai/auth/login"
API_BASE  = "https://api.geminigen.ai/api"
HIDE_WINDOW = False

TURNSTILE_SITEKEY  = "0x4AAAAAACDBydnKT0zYzh2H"
TURNSTILE_PAGE_URL = "https://geminigen.ai/auth/login"

GENERATE_TIMEOUT    = 600
API_POLL_INTERVAL   = 15
CAPSOLVER_MAX_RETRY = 3
TOKEN_CACHE_TTL     = 180   # 秒，token 有效期
HEARTBEAT_INTERVAL  = 60    # 秒，心跳间隔（缩短至 60s）
TOKEN_PREREFRESH    = 60    # 秒，提前多少秒主动刷新（TTL-60=120s 时触发）
NET_RETRY_COUNT     = 3
NET_RETRY_DELAY     = 5

_IMAGE_FORMAT_ERROR_CODES = (
    "IMAGE_FORMAT","INVALID_IMAGE","IMAGE_PARSE","UNSUPPORTED_IMAGE",
    "IMAGE_ERROR","FORMAT_ERROR","PARSE_ERROR","IMAGE_INVALID",
    "UNSUPPORTED_FORMAT","IMAGE_DECODE","IMAGE_READ",
)
_IMAGE_FORMAT_ERROR_MSGS = (
    "image format","invalid image","parse error","unsupported format",
    "image parse","decode error","unable to read","cannot read image",
    "图片格式","解析错误","格式错误","不支持的格式","图片解析","无法读取",
)
_BROWSER_DEAD_KEYWORDS = (
    "Target page, context or browser has been closed",
    "Browser has been closed","browser has been disconnected",
    "Target closed","Page closed","Connection refused",
    "Connection closed","socket hang up","ECONNREFUSED","10061",
)


def _is_image_format_error(ec, em):
    ec = (ec or "").upper(); em = (em or "").lower()
    return (any(k in ec for k in _IMAGE_FORMAT_ERROR_CODES) or
            any(k in em for k in _IMAGE_FORMAT_ERROR_MSGS))

def _is_browser_dead_error(exc):
    return any(k in str(exc) for k in _BROWSER_DEAD_KEYWORDS)

def _is_network_error(exc):
    kw = ("ConnectionReset","10054","Connection aborted","ConnectionError",
          "RemoteDisconnected","timeout","Timeout","ECONNRESET")
    return any(k in str(exc) for k in kw) or isinstance(exc, (ReqConnectionError, Timeout))

def set_account(username, password, instance_idx=0):
    global USERNAME, PASSWORD, _instance_idx
    USERNAME = username; PASSWORD = password; _instance_idx = instance_idx
    logger.info(f"账号已设置: {username}  实例ID: {instance_idx}")

def _get_profile_dir():
    h = hashlib.md5(USERNAME.encode()).hexdigest()[:12]
    return os.path.join(SCRIPT_DIR, f"playwright_profile_{h}")

def rsleep(a, b): time.sleep(random.uniform(a, b))


# ============================================================
# 任务注册表（多 Worker 共享，与浏览器无关）
# ============================================================
_registry_lock = threading.Lock()
_task_registry = {}

def _register_task(uuid):
    event = threading.Event()
    with _registry_lock:
        _task_registry[uuid] = {
            "event": event, "status": None,
            "thumbnail_url": None, "error_code": "", "error_message": ""
        }
    return event

def _unregister_task(uuid):
    with _registry_lock: _task_registry.pop(uuid, None)

def _scan_and_notify(results):
    with _registry_lock:
        for item in results:
            uuid = item.get("uuid")
            if not uuid or uuid not in _task_registry: continue
            entry = _task_registry[uuid]
            if entry["event"].is_set(): continue
            status = item.get("status")
            if status in (2, 3):
                entry["status"]        = status
                entry["thumbnail_url"] = item.get("thumbnail_url")
                entry["error_code"]    = item.get("error_code", "")
                entry["error_message"] = item.get("error_message", "")
                entry["event"].set()

def _get_task_result(uuid):
    with _registry_lock:
        e = _task_registry.get(uuid)
        return dict(e) if e else None


# ============================================================
# Turnstile 拦截脚本
# ============================================================
_TURNSTILE_INTERCEPT_SCRIPT = """
(function() {
    window.__ts_cb = null; window.__ts_wid = null; window.__ts_patched = false;
    function _patch() {
        if (!window.turnstile || window.__ts_patched) return;
        window.__ts_patched = true;
        var _origRender = window.turnstile.render.bind(window.turnstile);
        window.turnstile.render = function(container, params) {
            if (params && typeof params.callback === 'function') {
                window.__ts_cb = params.callback;
                console.log('[TS-PATCH] 已捕获 callback');
            }
            var wid = _origRender(container, params);
            window.__ts_wid = wid; return wid;
        };
        var _origGR = window.turnstile.getResponse.bind(window.turnstile);
        window.turnstile.getResponse = function(id) {
            if (window.__ts_injected_token) return window.__ts_injected_token;
            return _origGR(id);
        };
        console.log('[TS-PATCH] turnstile.render 已劫持');
    }
    _patch();
    var _tid = setInterval(function() { _patch(); if (window.__ts_patched) clearInterval(_tid); }, 50);
    setTimeout(function() { clearInterval(_tid); }, 15000);
    window.__inject_ts = function(token) {
        window.__ts_injected_token = token; var ok = false;
        if (typeof window.__ts_cb === 'function') {
            try { window.__ts_cb(token); ok = true; } catch(e) {}
        }
        document.querySelectorAll('input[name="cf-turnstile-response"], input[id*="response"]').forEach(function(el) {
            try {
                var setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
                setter.call(el, token);
                el.dispatchEvent(new Event('input',  {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
            } catch(e) {}
        });
        try {
            window.dispatchEvent(new MessageEvent('message', {
                data: JSON.stringify({ source:'cloudflare', widgetId: window.__ts_wid||'auto',
                    eventData:{event:'verify', token: token} }),
                origin: 'https://challenges.cloudflare.com'
            }));
        } catch(e) {}
        return ok;
    };
})();
"""


# ============================================================
# 浏览器辅助函数（仅在 TokenManager 线程调用）
# ============================================================
def _is_logged_in(page):
    try:
        auth = page.evaluate("localStorage.getItem('authStore')")
        if auth:
            data = json.loads(auth)
            return bool(data.get("access_token"))
        return False
    except: return False

def _close_popup(page):
    try:
        for text in ("不恢复","Cancel","No thanks","Dismiss","OK","Ok","Got it","Close","关闭","确定"):
            try:
                btn = page.locator(f"button:has-text('{text}')").first
                if btn.is_visible(timeout=500):
                    btn.click(); rsleep(0.3, 0.6); return
            except: pass
    except: pass

def _do_login(page: Page) -> bool:
    page.add_init_script(_TURNSTILE_INTERCEPT_SCRIPT)
    logger.info(f"  打开登录页: {LOGIN_URL}")
    try:
        page.goto(LOGIN_URL, timeout=60000)
        page.wait_for_load_state("domcontentloaded")
    except Exception as e:
        logger.error(f"  ❌ 打开登录页失败: {e}"); return False

    rsleep(2.0, 3.0); _close_popup(page)

    # 填邮箱
    email_filled = False
    for sel in ['input[name="username"]', 'input[name="email"]',
                'input[type="email"]', 'input[placeholder*="email" i]']:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=2000):
                el.fill(USERNAME); rsleep(0.5, 1.0)
                email_filled = True
                logger.info(f"  邮箱已填入（{sel}）"); break
        except: continue
    if not email_filled:
        logger.error("  ❌ 找不到邮箱输入框"); return False

    # 填密码
    pwd_filled = False
    for sel in ['input[name="password"]', 'input[type="password"]']:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=2000):
                el.fill(PASSWORD); rsleep(0.8, 1.5)
                pwd_filled = True; break
        except: continue
    if not pwd_filled:
        logger.error("  ❌ 找不到密码输入框"); return False

    # CapSolver 解 Turnstile
    logger.info("  解 Turnstile（CapSolver）...")
    try:
        ts_token = _solve_turnstile()
        logger.info(f"  ✅ Turnstile token（{len(ts_token)}字符）")
    except Exception as e:
        logger.error(f"  ❌ Turnstile 失败: {e}"); return False

    # 注入 token
    try:
        ok = page.evaluate(f"window.__inject_ts('{ts_token}')")
        logger.info(f"  注入结果: callback已调用={ok}")
    except Exception as e:
        logger.warning(f"  注入异常: {e}")
    rsleep(1.5, 2.5)

    try:
        btn_disabled = page.evaluate("document.querySelector('button[type=\"submit\"]')?.disabled")
        logger.info(f"  按钮 disabled={btn_disabled}")
    except: pass

    # 点击 Continue
    submitted = False
    try:
        btn = page.locator('button[type="submit"]').first
        btn.click(timeout=10000, force=True)
        submitted = True; logger.info("  已点击 Continue")
    except Exception as e:
        logger.warning(f"  按钮点击失败: {e}")
    if not submitted:
        try:
            page.locator('input[name="password"], input[type="password"]').first.press("Enter")
            submitted = True; logger.info("  已按 Enter 提交")
        except: pass
    if not submitted:
        logger.error("  ❌ 无法提交表单"); return False

    # 等待登录成功
    logger.info("  等待登录结果...")
    deadline = time.time() + 60
    while time.time() < deadline:
        rsleep(2.0, 3.0)
        try:
            cur_url = page.url or ""
            for sel in ['[class*="error"]', '[role="alert"]']:
                try:
                    for el in page.locator(sel).all():
                        txt = (el.text_content() or "").strip().lower()
                        if txt and any(k in txt for k in
                            ["invalid","incorrect","wrong","failed","密码错误","登录失败"]):
                            logger.error(f"  ❌ 登录错误: {txt[:80]}"); return False
                except: pass
            if "geminigen.ai" in cur_url and "/auth" not in cur_url:
                rsleep(2.0, 3.0)
                if _is_logged_in(page):
                    logger.info(f"  ✅ 登录成功 (URL={cur_url[:60]})"); return True
                logger.info("  URL 已跳转但 token 未就绪，继续等待...")
        except Exception as e:
            if _is_browser_dead_error(e): raise
            logger.warning(f"  等待时异常: {e}")
    logger.error("  ❌ 登录超时"); return False

def _extract_token_from_browser(page):
    for attempt in range(6):
        try:
            data = page.evaluate("""
                (() => {
                    var token='', gid=localStorage.getItem('guard_stable_id')||'';
                    var auth=localStorage.getItem('authStore');
                    if(auth){ try{ token=JSON.parse(auth).access_token||''; }catch(e){} }
                    return { access_token: token, guard_id: gid };
                })()
            """)
            token = data.get("access_token", ""); gid = data.get("guard_id", "")
            if token: return token, gid
            logger.warning(f"  提取第{attempt+1}次: token 为空")
            rsleep(3.0, 5.0)
        except Exception as e:
            logger.error(f"  提取 token 异常: {e}"); rsleep(2.0, 3.0)
    return None, None


# ============================================================
# TokenManager —— 专用守护线程，唯一允许操作浏览器的角色
# ============================================================
class _TokenManager(threading.Thread):
    """
    fire-and-forget 刷新机制：
    - Worker 发现 token 失效 → 调 get_token(force_refresh=True, stale_token=xxx)
      → TokenManager 在后台开始刷新，立即返回 (None, None) 给 Worker
      → Worker 自己 sleep 后重试普通 get_token()
    - Worker 普通调 get_token() → 若刷新在进行中则等待（最长 300s），完成后返回
    - 心跳每 60s 一次，token 剩余不足 60s 时提前主动刷新
    """

    def __init__(self):
        super().__init__(name="TokenManager", daemon=True)

        # Token 状态
        self._token      = None
        self._gid        = None
        self._token_time = 0.0
        self._state_lock = threading.Lock()

        # 被服务器拒绝的坏 token 集合
        self._bad_tokens: set = set()

        # 是否正在刷新（防重入）
        self._refreshing = False

        # 信号
        self._token_ready     = threading.Event()   # 有可用 token 时置位
        self._refresh_request = threading.Event()   # 有刷新请求时置位
        self._stop_event      = threading.Event()

        # 浏览器（仅本线程访问）
        self._pw:      Playwright     = None
        self._context: BrowserContext = None
        self._page:    Page           = None

    # ── Worker 公共接口 ────────────────────────────────────────

    def get_token(self, force_refresh=False, stale_token=None, timeout=300):
        """
        force_refresh=True  → 非阻塞：通知刷新，立即返回 (None, None)，Worker 自己重试
        force_refresh=False → 阻塞等待：有 token 直接返回，正在刷新则等最多 timeout 秒
        """
        with self._state_lock:
            if force_refresh and stale_token:
                # 记录坏 token
                self._bad_tokens.add(stale_token)
                # 如果当前持有的是一个更新的有效 token，直接返回
                if self._token and self._token not in self._bad_tokens:
                    logger.debug("TokenManager: 已有更新 token，直接返回")
                    return self._token, self._gid
                # 触发后台刷新，立即返回 None（Worker 自己 sleep 重试）
                if not self._refreshing:
                    self._token_ready.clear()
                    self._refresh_request.set()
                    logger.info("TokenManager: 收到 token 失效通知，后台刷新已触发")
                return None, None   # ← 非阻塞，Worker 重试

            # 普通获取：有有效 token 直接返回
            if (self._token and
                    self._token not in self._bad_tokens and
                    time.time() - self._token_time < TOKEN_CACHE_TTL):
                return self._token, self._gid

            # 没有有效 token，触发刷新（如果还没在刷）
            if not self._refreshing:
                self._token_ready.clear()
                self._refresh_request.set()

        # 等待刷新完成（最长 timeout 秒）
        if self._token_ready.wait(timeout=timeout):
            with self._state_lock:
                if self._token and self._token not in self._bad_tokens:
                    return self._token, self._gid
        logger.error(f"TokenManager.get_token: 等待超时({timeout}s)")
        return None, None

    def invalidate(self, bad_token=None):
        """Worker 通过 HTTP 层发现 token 无效时调用（非阻塞通知）"""
        with self._state_lock:
            if bad_token:
                self._bad_tokens.add(bad_token)
            self._token      = None
            self._token_time = 0.0
            if not self._refreshing:
                self._token_ready.clear()
                self._refresh_request.set()

    def stop(self):
        self._stop_event.set()
        self._refresh_request.set()

    # ── 主循环 ────────────────────────────────────────────────

    def run(self):
        logger.info("TokenManager: 线程启动，正在初始化浏览器...")
        try:
            self._create_context()
        except Exception as e:
            logger.error(f"TokenManager: 浏览器初始化失败: {e}")
            self._token_ready.set(); return

        # 首次刷新（含登录）
        self._do_refresh(is_first=True)

        while not self._stop_event.is_set():
            triggered = self._refresh_request.wait(timeout=HEARTBEAT_INTERVAL)
            if self._stop_event.is_set(): break
            if triggered:
                self._refresh_request.clear()
                self._do_refresh()
            else:
                self._heartbeat()

        self._cleanup_browser()
        logger.info("TokenManager: 线程已退出")

    # ── Token 刷新 ────────────────────────────────────────────

    def _do_refresh(self, is_first=False):
        """
        核心刷新流程，仅在 TokenManager 线程执行。
        始终 reload 页面（让 SPA 用 refresh_token 换新 access_token）；
        若 reload 后拿到的仍是坏 token，执行完整重新登录。
        """
        with self._state_lock:
            if self._refreshing:
                logger.debug("TokenManager: 刷新已在进行中，跳过重复调用")
                return
            self._refreshing  = True
            self._token_ready.clear()
            bad_snapshot = set(self._bad_tokens)

        logger.info(f"TokenManager: 开始刷新 Token（首次={is_first}）...")

        try:
            for attempt in range(3):
                try:
                    # 检查/重建浏览器
                    if not self._is_context_alive():
                        logger.warning(f"TokenManager: 浏览器已死（第{attempt+1}次），重建...")
                        self._rebuild_context()
                        if self._context is None:
                            time.sleep(5); continue

                    page = self._page

                    # ── 首次进入：直接导航到 App ──
                    if is_first:
                        logger.info("TokenManager: 首次导航到 App 页面...")
                        try:
                            page.goto(APP_URL, timeout=60000)
                            page.wait_for_load_state("domcontentloaded")
                            rsleep(3.0, 5.0); _close_popup(page)
                        except Exception as e:
                            if _is_browser_dead_error(e):
                                self._rebuild_context()
                                if self._context is None: continue
                                page = self._page
                                page.goto(APP_URL, timeout=60000)
                                page.wait_for_load_state("domcontentloaded")
                                rsleep(3.0, 5.0); _close_popup(page)
                            else:
                                logger.error(f"TokenManager: 首次导航失败: {e}"); continue
                    else:
                        # ── 非首次：始终 reload，让 SPA 刷新 token ──
                        try:
                            cur = page.url or ""
                            if "geminigen.ai" not in cur:
                                logger.info("TokenManager: 页面不在 app，重新导航...")
                                page.goto(APP_URL, timeout=60000)
                                page.wait_for_load_state("domcontentloaded")
                                rsleep(3.0, 5.0); _close_popup(page)
                            else:
                                logger.info("TokenManager: reload 页面，等待 SPA 刷新 Token...")
                                page.reload(timeout=60000)
                                page.wait_for_load_state("domcontentloaded")
                                rsleep(3.0, 5.0); _close_popup(page)
                        except Exception as e:
                            if _is_browser_dead_error(e):
                                self._rebuild_context()
                                if self._context is None: continue
                                page = self._page
                            try:
                                page.goto(APP_URL, timeout=60000)
                                page.wait_for_load_state("domcontentloaded")
                                rsleep(3.0, 5.0); _close_popup(page)
                            except Exception as e2:
                                logger.error(f"TokenManager: 导航失败: {e2}"); continue

                    # ── 检查是否需要登录 ──
                    needs_login = False
                    try:
                        cur = page.url or ""
                        if "/auth" in cur or "/login" in cur:
                            needs_login = True
                        elif not _is_logged_in(page):
                            needs_login = True
                    except Exception as ce:
                        if _is_browser_dead_error(ce):
                            self._rebuild_context()
                            if self._context is None: continue
                            page = self._page
                        needs_login = True

                    if needs_login:
                        logger.info("TokenManager: ⚠ 需要登录...")
                        try:
                            login_ok = _do_login(page)
                        except Exception as login_e:
                            if _is_browser_dead_error(login_e):
                                logger.warning("TokenManager: 登录中浏览器死掉，重建后重试...")
                                self._rebuild_context()
                                if self._context is None: continue
                                page = self._page
                                try: login_ok = _do_login(page)
                                except Exception: continue
                            else:
                                logger.error(f"TokenManager: 登录异常: {login_e}"); continue
                        if not login_ok:
                            logger.error("TokenManager: 登录失败"); continue

                    # ── 提取 token ──
                    token, gid = _extract_token_from_browser(page)

                    if not token:
                        logger.warning(f"TokenManager: 提取 Token 为空（第{attempt+1}次）")
                        time.sleep(5); continue

                    # ── 验证：不能是已知坏 token ──
                    if token in bad_snapshot:
                        logger.warning(
                            f"TokenManager: reload 后拿到的仍是坏 token（第{attempt+1}次），"
                            "清除 localStorage 强制重登录..."
                        )
                        try: page.evaluate("localStorage.removeItem('authStore')")
                        except Exception: pass
                        try:
                            page.goto(LOGIN_URL, timeout=60000)
                            page.wait_for_load_state("domcontentloaded")
                            rsleep(2.0, 3.0)
                            login_ok = _do_login(page)
                        except Exception as e:
                            logger.error(f"TokenManager: 强制重登录异常: {e}"); continue
                        if not login_ok:
                            logger.error("TokenManager: 强制重登录失败"); continue
                        token, gid = _extract_token_from_browser(page)
                        if not token or token in bad_snapshot:
                            logger.error("TokenManager: 重登录后 token 仍无效"); continue

                    # ── 成功 ──
                    logger.info(f"TokenManager: ✅ 获得新 Token: {token[:30]}...")
                    self._set_token(token, gid)
                    return

                except Exception as e:
                    logger.error(f"TokenManager: 刷新异常（第{attempt+1}次）: {e}")
                    traceback.print_exc(); time.sleep(5)

            logger.error("TokenManager: ❌ Token 刷新连续失败3次")

        finally:
            with self._state_lock:
                self._refreshing = False
            # 无论成败都 set，防止 Worker 永久阻塞
            self._token_ready.set()

    def _heartbeat(self):
        """定期主动检测 token 健康状态"""
        with self._state_lock:
            token     = self._token
            token_age = time.time() - self._token_time
            is_bad    = (token in self._bad_tokens) if token else True

        if not token or is_bad:
            logger.info("TokenManager: 心跳发现无有效 Token，触发刷新...")
            with self._state_lock:
                if not self._refreshing:
                    self._token_ready.clear()
                    self._refresh_request.set()
            return

        # 提前 TOKEN_PREREFRESH 秒主动刷新
        if token_age > TOKEN_CACHE_TTL - TOKEN_PREREFRESH:
            logger.info(
                f"TokenManager: 心跳发现 Token 即将过期"
                f"（已用{token_age:.0f}s / TTL={TOKEN_CACHE_TTL}s），主动刷新..."
            )
            with self._state_lock:
                if not self._refreshing:
                    self._token_ready.clear()
                    self._refresh_request.set()
            return

        # 轻量 HTTP 探测
        try:
            headers = _build_headers(token)
            resp = req_lib.get(
                f"{API_BASE}/histories?items_per_page=1&page=1",
                headers=headers, timeout=15,
                proxies={"http": None, "https": None}
            )
            if resp.status_code in (401, 403):
                logger.warning(
                    f"TokenManager: 心跳 HTTP {resp.status_code}，token 已失效，触发刷新..."
                )
                with self._state_lock:
                    if token: self._bad_tokens.add(token)
                    self._token = None; self._token_time = 0.0
                    if not self._refreshing:
                        self._token_ready.clear()
                        self._refresh_request.set()
            else:
                logger.debug(f"TokenManager: 心跳 OK（HTTP {resp.status_code}）")
        except Exception as e:
            logger.debug(f"TokenManager: 心跳网络异常（忽略）: {e}")

    def _set_token(self, token, gid):
        with self._state_lock:
            self._token      = token
            self._gid        = gid
            self._token_time = time.time()
            self._bad_tokens.clear()   # 新 token 成功，清空坏记录
        self._token_ready.set()

    # ── 浏览器管理（仅本线程）────────────────────────────────

    def _create_context(self):
        profile_dir    = _get_profile_dir()
        is_new_profile = not os.path.exists(profile_dir)
        os.makedirs(profile_dir, exist_ok=True)
        if self._pw is None:
            self._pw = sync_playwright().start()
        logger.info(f"TokenManager: {'新建' if is_new_profile else '复用'}浏览器实例 账号={USERNAME}")
        logger.info(f"TokenManager: Profile 目录: {profile_dir}")
        ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=HIDE_WINDOW,
            args=[
                "--no-sandbox","--disable-blink-features=AutomationControlled",
                "--lang=zh-CN,zh,en-US,en","--window-size=1920,1080",
                "--disable-extensions","--disable-translate",
            ],
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN", ignore_https_errors=True,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
            ),
        )
        ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins',   { get: () => [1,2,3,4,5] });
            window.chrome = { runtime: {} };
        """)
        self._context = ctx
        self._page    = ctx.pages[0] if ctx.pages else ctx.new_page()
        logger.info("TokenManager: ✅ 浏览器实例已就绪")

    def _is_context_alive(self):
        if self._context is None or self._page is None: return False
        try: _ = self._page.url; return True
        except: return False

    def _rebuild_context(self):
        logger.warning("TokenManager: ⚠ 浏览器连接断开，正在重建...")
        for obj in [self._page, self._context]:
            if obj:
                try: obj.close()
                except: pass
        self._page = self._context = None
        with self._state_lock:
            self._token = None; self._gid = None; self._token_time = 0.0
        try:
            self._create_context()
            logger.info("TokenManager: ✅ 浏览器重建成功")
        except Exception as e:
            logger.error(f"TokenManager: ❌ 浏览器重建失败: {e}")
            self._context = self._page = None

    def _cleanup_browser(self):
        for obj in [self._page, self._context]:
            if obj:
                try: obj.close()
                except: pass
        if self._pw:
            try: self._pw.stop()
            except: pass
        self._pw = self._context = self._page = None
        logger.info("TokenManager: 浏览器已关闭")


# ============================================================
# 全局单例
# ============================================================
_token_manager: _TokenManager = None
_token_manager_lock = threading.Lock()

def _get_token_manager() -> _TokenManager:
    global _token_manager
    with _token_manager_lock:
        if _token_manager is None:
            raise RuntimeError("TokenManager 尚未初始化，请先调用 init_login()")
        return _token_manager


# ============================================================
# Worker 调用的 Token 接口
# ============================================================
def _get_fresh_token(force_refresh=False, stale_token=None):
    """
    Worker 获取 Token 的唯一入口。
    force_refresh=True  → 非阻塞，通知刷新后立即返回 None，Worker 自己 sleep 重试
    force_refresh=False → 阻塞等待，最长 300s
    """
    try:
        return _get_token_manager().get_token(
            force_refresh=force_refresh,
            stale_token=stale_token,
            timeout=300
        )
    except RuntimeError as e:
        logger.error(f"_get_fresh_token: {e}")
        return None, None

def _invalidate_token_cache(bad_token=None):
    """Worker 在网络层发现 token 无效时调用（非阻塞通知）"""
    try:
        _get_token_manager().invalidate(bad_token=bad_token)
    except RuntimeError:
        pass


# ============================================================
# 初始化 / 退出
# ============================================================
def init_login() -> bool:
    global _token_manager
    logger.info("=" * 40)
    logger.info("初始化：启动 TokenManager，等待首次登录...")
    logger.info("=" * 40)
    with _token_manager_lock:
        if _token_manager is not None and _token_manager.is_alive():
            logger.info("TokenManager 已在运行，跳过重复初始化")
            return True
        _token_manager = _TokenManager()
        _token_manager.start()

    # 等待首次 token 就绪（最长 300s，含登录流程）
    if _token_manager._token_ready.wait(timeout=300):
        token, _ = _token_manager.get_token(timeout=10)
        if token:
            logger.info("✅ 初始化完成，登录状态正常")
            return True
    logger.error("❌ 初始化失败：TokenManager 启动超时或登录失败")
    return False

def quit_driver():
    global _token_manager
    with _token_manager_lock:
        if _token_manager is not None:
            _token_manager.stop()
            _token_manager.join(timeout=15)
            _token_manager = None
    logger.info("TokenManager 已停止")


# ============================================================
# HTTP 工具
# ============================================================
def _safe_get(url, headers, timeout=30):
    for a in range(1, NET_RETRY_COUNT + 1):
        try: return req_lib.get(url, headers=headers, timeout=timeout)
        except (ReqConnectionError, Timeout, ConnectionError, OSError) as e:
            if a < NET_RETRY_COUNT:
                logger.warning(f"  网络错误(GET第{a}次): {type(e).__name__}, {NET_RETRY_DELAY}s后重试...")
                time.sleep(NET_RETRY_DELAY)
            else: raise

def _safe_post_json(url, json_data, timeout=30):
    for a in range(1, NET_RETRY_COUNT + 1):
        try: return req_lib.post(url, json=json_data, timeout=timeout)
        except (ReqConnectionError, Timeout, ConnectionError, OSError) as e:
            if a < NET_RETRY_COUNT:
                logger.warning(f"  网络错误(POST第{a}次): {type(e).__name__}, {NET_RETRY_DELAY}s后重试...")
                time.sleep(NET_RETRY_DELAY)
            else: raise

def _safe_post_files(url, headers, files, timeout=120):
    for a in range(1, NET_RETRY_COUNT + 1):
        try: return req_lib.post(url, headers=headers, files=files, timeout=timeout)
        except (ReqConnectionError, Timeout, ConnectionError, OSError) as e:
            if a < NET_RETRY_COUNT:
                logger.warning(f"  网络错误(上传第{a}次): {type(e).__name__}, {NET_RETRY_DELAY}s后重试...")
                time.sleep(NET_RETRY_DELAY)
                for _, ft in files:
                    fobj = ft[1] if isinstance(ft, tuple) else ft
                    if hasattr(fobj, "seek"): fobj.seek(0)
            else: raise

def _build_headers(token, gid=""):
    h = {
        "accept":             "*/*",
        "accept-language":    "zh-CN,zh;q=0.9",
        "authorization":      f"Bearer {token}",
        "origin":             "https://geminigen.ai",
        "referer":            "https://geminigen.ai/",
        "sec-ch-ua":          '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"',
        "sec-ch-ua-mobile":   "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest":     "empty",
        "sec-fetch-mode":     "cors",
        "sec-fetch-site":     "same-site",
        "user-agent":         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    }
    if gid: h["x-guard-id"] = gid
    return h


# ============================================================
# CapSolver（纯 HTTP，任意线程可调用）
# ============================================================
def _solve_turnstile():
    for attempt in range(1, CAPSOLVER_MAX_RETRY + 1):
        try:
            logger.info(f"  CapSolver（第{attempt}次）...")
            payload = {
                "clientKey": CAPSOLVER_API_KEY,
                "task": {
                    "type":       "AntiTurnstileTaskProxyLess",
                    "websiteURL": TURNSTILE_PAGE_URL,
                    "websiteKey": TURNSTILE_SITEKEY,
                },
            }
            resp   = _safe_post_json("https://api.capsolver.com/createTask", payload, timeout=30)
            result = resp.json()
            if result.get("errorId", 0) != 0:
                raise RuntimeError(f"创建失败: {result.get('errorDescription')}")
            task_id = result["taskId"]
            for i in range(60):
                time.sleep(3)
                resp = _safe_post_json(
                    "https://api.capsolver.com/getTaskResult",
                    {"clientKey": CAPSOLVER_API_KEY, "taskId": task_id}, timeout=30
                )
                r = resp.json()
                if r.get("status") == "ready":
                    token = r["solution"]["token"]
                    logger.info(f"  ✅ Turnstile 已解决 ({len(token)}字符)"); return token
                elif r.get("status") == "failed":
                    raise RuntimeError(f"失败: {r.get('errorDescription', r.get('errorCode'))}")
                elif r.get("status") == "processing":
                    if i % 5 == 0: logger.info(f"  等待 CapSolver... ({i*3}s)")
                else:
                    raise RuntimeError(f"异常状态: {r.get('status')}")
            raise RuntimeError("超时")
        except Exception as e:
            logger.warning(f"  CapSolver 第{attempt}次失败: {e}")
            if attempt < CAPSOLVER_MAX_RETRY: rsleep(3.0, 5.0)
            else: raise


# ============================================================
# API 提交
# ============================================================
def _api_generate(token, gid, scene_photo, product_image, turnstile_token, prompt_text,
                  model="nano-banana-2"):
    url     = f"{API_BASE}/generate_image"
    headers = _build_headers(token, gid)
    ratio   = random.choice(["3:4", "4:3"])
    logger.info(f"  纵横比: {ratio}")

    fields = [
        ("prompt",          (None, prompt_text)),
        ("model",           (None, model)),
        ("aspect_ratio",    (None, ratio)),
        ("output_format",   (None, "png")),
        ("resolution",      (None, "1K")),
        ("turnstile_token", (None, turnstile_token)),
    ]
    file_handles = []
    for img_path in [scene_photo, product_image]:
        fname = os.path.basename(img_path)
        mime  = mimetypes.guess_type(img_path)[0] or "image/png"
        fh    = open(img_path, "rb")
        file_handles.append(fh)
        fields.append(("files", (fname, fh, mime)))
    try:
        resp = _safe_post_files(url, headers, fields, timeout=120)
    finally:
        for fh in file_handles: fh.close()

    logger.info(f"  API响应: HTTP {resp.status_code}")
    if resp.status_code == 200:
        data      = resp.json()
        logger.info(f"  {json.dumps(data, ensure_ascii=False)[:150]}")
        task_uuid = data.get("uuid")
        if not task_uuid:
            uuids = re.findall(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                json.dumps(data)
            )
            task_uuid = uuids[0] if uuids else None
        if task_uuid: logger.info(f"  ✅ UUID: {task_uuid}")
        return resp.status_code, task_uuid, ""
    else:
        error_code = ""
        try:
            err_data   = resp.json()
            error_code = err_data.get("detail", {}).get("error_code", "")
            error_msg  = err_data.get("detail", {}).get("message", "") or resp.text
            if _is_image_format_error(error_code, error_msg):
                logger.error(f"  ❌ 图片格式/解析错误: code={error_code}")
                return resp.status_code, None, "IMAGE_FORMAT_ERROR"
        except: pass
        logger.error(f"  ❌ HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.status_code, None, error_code


# ============================================================
# API 轮询 + 下载
# ============================================================
def _api_poll_and_download(task_uuid, save_path):
    event = _register_task(task_uuid)
    consecutive_net_errors = 0
    try:
        start = time.time(); got_4xx = False
        current_token = current_gid = None
        stale_token_to_report = None

        while time.time() - start < GENERATE_TIMEOUT:
            elapsed = int(time.time() - start)

            # 检查其他 Worker 是否已发现结果
            if event.is_set():
                result = _get_task_result(task_uuid)
                if result:
                    if result["status"] == 2 and result["thumbnail_url"]:
                        logger.info("  ✅ 生成完成（被其他 worker 发现）!")
                        dl_ok = _download_image(result["thumbnail_url"], save_path)
                        return (dl_ok, result["thumbnail_url"], "")
                    elif result["status"] == 3:
                        ec = result.get("error_code", "") or ""
                        em = result.get("error_message", "") or ""
                        logger.error(f"  ❌ 任务失败: {ec} - {em}")
                        if _is_image_format_error(ec, em): return (False, None, "IMAGE_FORMAT_ERROR")
                        return (False, None, "")

            # 获取 token
            if got_4xx:
                # 非阻塞通知刷新，自己 sleep 等一会儿
                _get_fresh_token(force_refresh=True, stale_token=stale_token_to_report)
                got_4xx = False
                stale_token_to_report = None
                logger.info("  Token 失效已通知 TokenManager，等待15s后重新获取...")
                time.sleep(15)
                current_token, current_gid = _get_fresh_token()   # 阻塞等新 token
            else:
                current_token, current_gid = _get_fresh_token()

            if not current_token:
                logger.error("  无法获取 token，等待15秒后重试...")
                time.sleep(15); consecutive_net_errors += 1
                if consecutive_net_errors >= 5:
                    logger.error("  ❌ 连续5次无法获取 token，放弃")
                    return (False, None, "")
                continue
            else:
                consecutive_net_errors = 0

            try:
                headers     = _build_headers(current_token, current_gid)
                all_results = []; found_task = False
                for page_num in range(1, 4):
                    resp = _safe_get(
                        f"{API_BASE}/histories?items_per_page=50&page={page_num}",
                        headers=headers, timeout=30
                    )
                    if resp.status_code == 200:
                        data         = resp.json()
                        page_results = data.get("result", [])
                        all_results.extend(page_results)
                        _scan_and_notify(page_results)
                        if event.is_set(): found_task = True; break
                        for item in page_results:
                            if item.get("uuid") == task_uuid:
                                found_task = True
                                status     = item.get("status")
                                logger.info(f"  [{elapsed}s] status={status} (page={page_num})")
                                if status == 2:
                                    thumb_url = item.get("thumbnail_url")
                                    if thumb_url:
                                        dl_ok = _download_image(thumb_url, save_path)
                                        return (dl_ok, thumb_url, "")
                                elif status == 3:
                                    ec = item.get("error_code", "") or ""
                                    em = item.get("error_message", "") or ""
                                    logger.error(f"  ❌ 任务失败: {ec} - {em}")
                                    if _is_image_format_error(ec, em):
                                        return (False, None, "IMAGE_FORMAT_ERROR")
                                    return (False, None, "")
                                break
                        if found_task: break
                        if len(page_results) < 50: break
                    elif resp.status_code in (401, 403):
                        stale_token_to_report = current_token
                        got_4xx = True
                        logger.warning(f"  [{elapsed}s] HTTP {resp.status_code}，token 失效，将刷新")
                        break
                    elif resp.status_code in (500, 502, 503):
                        logger.warning(f"  [{elapsed}s] HTTP {resp.status_code} 服务器故障"); break
                    else:
                        logger.warning(f"  [{elapsed}s] HTTP {resp.status_code}"); break
                if not found_task and not got_4xx and not event.is_set():
                    logger.info(f"  [{elapsed}s] 任务尚未出现（已查{len(all_results)}条）")
            except Exception as e:
                consecutive_net_errors += 1
                logger.warning(f"  [{elapsed}s] 轮询异常({consecutive_net_errors}次): {e}")
                if consecutive_net_errors >= 3:
                    logger.warning("  连续异常，失效 token 缓存，等待30秒...")
                    _invalidate_token_cache(bad_token=current_token)
                    time.sleep(30); consecutive_net_errors = 0
            event.wait(timeout=API_POLL_INTERVAL)

        logger.error(f"  ❌ 超时 ({GENERATE_TIMEOUT}s)")
        return (False, None, "")
    finally:
        _unregister_task(task_uuid)


def _download_image(url, save_path):
    logger.info(f"  下载: {url[:80]}...")
    try:
        headers = {
            "Referer":    "https://geminigen.ai/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/145 Safari/537.36"
        }
        resp = _safe_get(url, headers=headers, timeout=60)
        resp.raise_for_status()
        with open(save_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192): f.write(chunk)
        size_mb = os.path.getsize(save_path) / 1024 / 1024
        logger.info(f"  ✅ 已保存: {save_path} ({size_mb:.1f}MB)")
        return True
    except Exception as e:
        logger.error(f"  ❌ 下载失败: {e}")
        return False


# ============================================================
# 对外主接口
# ============================================================
def run_task(scene_photo, product_image, save_path, prompt_text, model="nano-banana-2"):
    MAX_SUBMIT_RETRY = 10
    QUEUE_FULL_WAIT  = 30
    try:
        task_uuid = None
        for submit_attempt in range(1, MAX_SUBMIT_RETRY + 1):

            # ── 获取 token ──
            logger.info("▷ 获取 Token...")
            token, gid = _get_fresh_token()
            if not token:
                logger.error("  Token 获取失败，等待10秒重试...")
                time.sleep(10); continue

            # ── 解 Turnstile ──
            logger.info("▷ 解 Turnstile...")
            try:
                turnstile_token = _solve_turnstile()
            except Exception as e:
                if _is_network_error(e):
                    logger.warning("  Turnstile 网络错误，等10秒重试...")
                    _invalidate_token_cache(); time.sleep(10); continue
                logger.error(f"  Turnstile 失败: {e}")
                return (False, None, "")

            # ── 提交 ──
            logger.info("▷ API 提交...")
            try:
                status_code, task_uuid, submit_error = _api_generate(
                    token, gid, scene_photo, product_image, turnstile_token, prompt_text, model
                )
            except Exception as e:
                if _is_network_error(e):
                    logger.warning(f"  API 提交网络错误，等10秒重试（第{submit_attempt}次）...")
                    _invalidate_token_cache(); time.sleep(10); continue
                raise

            if submit_error == "IMAGE_FORMAT_ERROR":
                return (False, None, "IMAGE_FORMAT_ERROR")
            if task_uuid:
                break

            if submit_error == "MAX_PROCESSING_IMAGEN_EXCEEDED":
                logger.warning(
                    f"  ⏳ 队列已满，等{QUEUE_FULL_WAIT}秒"
                    f"（第{submit_attempt}/{MAX_SUBMIT_RETRY}次）..."
                )
                time.sleep(QUEUE_FULL_WAIT); continue

            if status_code in (401, 403):
                logger.info("  Token 过期，通知刷新后重试...")
                # 非阻塞通知
                _get_fresh_token(force_refresh=True, stale_token=token)
                logger.info("  等待15秒后重新获取 token...")
                time.sleep(15)
                # 阻塞等新 token
                token, gid = _get_fresh_token()
                if not token: time.sleep(10); continue
                try:
                    turnstile_token = _solve_turnstile()
                except: continue
                try:
                    status_code, task_uuid, submit_error = _api_generate(
                        token, gid, scene_photo, product_image, turnstile_token, prompt_text, model
                    )
                except Exception as e:
                    if _is_network_error(e):
                        _invalidate_token_cache(); time.sleep(10); continue
                    raise
                if submit_error == "IMAGE_FORMAT_ERROR":
                    return (False, None, "IMAGE_FORMAT_ERROR")
                if task_uuid: break
                if submit_error == "MAX_PROCESSING_IMAGEN_EXCEEDED":
                    time.sleep(QUEUE_FULL_WAIT); continue

            logger.warning(
                f"  提交失败(HTTP {status_code})，等10秒重试（第{submit_attempt}次）..."
            )
            time.sleep(10)

        if not task_uuid:
            logger.error(f"  提交重试{MAX_SUBMIT_RETRY}次后仍失败")
            return (False, None, "")

        logger.info("▷ 等待结果...")
        return _api_poll_and_download(task_uuid, save_path)

    except Exception as e:
        logger.error(f"任务异常: {e}")
        traceback.print_exc()
        if _is_network_error(e):
            _invalidate_token_cache()
        return (False, None, "")