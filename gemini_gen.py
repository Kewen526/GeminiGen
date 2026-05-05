# -*- coding: utf-8 -*-
"""
GeminiGen.ai 自动化模块 v3（纯Python HTTP + GuardId 自计算）
================================================================
核心：
1. 通过 init_script 劫持 crypto.subtle.digest，捕获 hY（随机 secretKey）
2. 通过 init_script 拦截 XHR，从首次 API 请求中提取 domFp
3. GuardIdGenerator：一次捕获，Python 永久生成 x-guard-id
4. Turnstile 自适应：默认 skip，被拒后切换 CapSolver，30min 后自动回落
5. 无需上传任何图片，纯文字提交
"""

import base64
import hashlib
import json
import os
import re
import random
import struct
import time
import logging
import threading
import traceback

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
TOKEN_CACHE_TTL     = 180
HEARTBEAT_INTERVAL  = 60
TOKEN_PREREFRESH    = 60
NET_RETRY_COUNT     = 3
NET_RETRY_DELAY     = 5
RATE_LIMIT_COOLDOWN = 120

TURNSTILE_SKIP_FAIL_THRESHOLD = 2
SUBMIT_JITTER_MIN = 1.0
SUBMIT_JITTER_MAX = 4.0

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
_RATE_LIMIT_INDICATORS = (
    "rate_limit","too_many","rate limit","ratelimit",
    "请求过多","频率","too_many_requests","request_limit",
)

# ============================================================
# 实例级限速冷却
# ============================================================
_rate_limited_until: float = 0.0
_rate_limit_lock = threading.Lock()

# ============================================================
# Model 自适应状态机
# ============================================================
MODEL_PRIMARY         = "nano-banana-2"
MODEL_FALLBACK        = "nano-banana-pro"
MODEL_REVERT_INTERVAL = 1800

_current_model:     str   = MODEL_PRIMARY
_model_switched_at: float = 0.0
_model_lock = threading.Lock()

def _get_current_model() -> str:
    with _model_lock:
        return _current_model

def _on_model_rate_limited():
    global _current_model, _model_switched_at
    with _model_lock:
        now = time.time()
        revert_at = time.strftime('%H:%M:%S', time.localtime(now + MODEL_REVERT_INTERVAL))
        if _current_model == MODEL_PRIMARY:
            _current_model     = MODEL_FALLBACK
            _model_switched_at = now
            logger.warning(f"  [Model] {MODEL_PRIMARY} 触发限流，切换为 {MODEL_FALLBACK}，将于 {revert_at} 尝试回切")
        else:
            _model_switched_at = now
            logger.info(f"  [Model] 仍在限流，重置回切计时器，将于 {revert_at} 再次尝试回切")

def _check_model_auto_revert():
    global _current_model, _model_switched_at
    with _model_lock:
        if (_current_model == MODEL_FALLBACK
                and _model_switched_at > 0
                and time.time() - _model_switched_at >= MODEL_REVERT_INTERVAL):
            _current_model     = MODEL_PRIMARY
            _model_switched_at = 0.0
            logger.info(f"  [Model] 30分钟已过，回切尝试 {MODEL_PRIMARY}...")

# ============================================================
# Turnstile 自适应状态机
# ============================================================
TURNSTILE_RESET_INTERVAL = 1800

_turnstile_skip_fail_count: int  = 0
_turnstile_use_capsolver:  bool  = False
_turnstile_reset_at:       float = 0.0
_turnstile_lock = threading.Lock()

def _turnstile_on_skip_fail():
    global _turnstile_skip_fail_count, _turnstile_use_capsolver, _turnstile_reset_at
    with _turnstile_lock:
        _turnstile_skip_fail_count += 1
        if (not _turnstile_use_capsolver and
                _turnstile_skip_fail_count >= TURNSTILE_SKIP_FAIL_THRESHOLD):
            _turnstile_use_capsolver = True
            _turnstile_reset_at      = time.time() + TURNSTILE_RESET_INTERVAL
            logger.warning(
                f"  [Turnstile] skip 连续失败 {_turnstile_skip_fail_count} 次，"
                f"切换 CapSolver 模式，将于 "
                f"{time.strftime('%H:%M:%S', time.localtime(_turnstile_reset_at))} 重试 skip"
            )

def _turnstile_on_success():
    global _turnstile_skip_fail_count
    with _turnstile_lock:
        _turnstile_skip_fail_count = 0

def _get_turnstile_token() -> str:
    global _turnstile_use_capsolver, _turnstile_skip_fail_count, _turnstile_reset_at
    with _turnstile_lock:
        if _turnstile_use_capsolver and time.time() >= _turnstile_reset_at:
            _turnstile_use_capsolver   = False
            _turnstile_skip_fail_count = 0
            logger.info("  [Turnstile] 定时重置，重新尝试 skip 模式")
        need_capsolver = _turnstile_use_capsolver

    if not need_capsolver:
        logger.debug("  [Turnstile] 使用 skip")
        return "skip"
    logger.info("  [Turnstile] 使用 CapSolver 解码...")
    return _solve_turnstile()

def _trigger_rate_limit():
    global _rate_limited_until
    with _rate_limit_lock:
        new_until = time.time() + RATE_LIMIT_COOLDOWN
        if new_until > _rate_limited_until:
            _rate_limited_until = new_until
            logger.warning(
                f"  [限速冷却] 实例暂停提交 {RATE_LIMIT_COOLDOWN}s，"
                f"截止 {time.strftime('%H:%M:%S', time.localtime(new_until))}"
            )

def _wait_for_rate_limit():
    while True:
        with _rate_limit_lock:
            remaining = _rate_limited_until - time.time()
        if remaining <= 0:
            return
        logger.warning(f"  [限速冷却] 还需等待 {remaining:.0f}s...")
        time.sleep(min(remaining, 10))

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
# GuardIdGenerator — 纯 Python 生成 x-guard-id
# ============================================================
class GuardIdGenerator:
    """
    逆向自 geminigen.ai/_nuxt/D6qDEc1Pjs 的 ab() 函数。
    算法（85字节）：
      [0]      = 0x01 (版本常量)
      [1:17]   = SHA256(hy:stable_id)[:32 hex] → 16字节（固定 per session）
      [17:21]  = timeBucket uint32 big-endian（每60s变化一次）
      [21:53]  = SHA256(path:METHOD:u:timeBucket:hy) → 32字节（per请求）
      [53:85]  = domFp → 32字节（CSS指纹，固定 per 浏览器）
    """
    def __init__(self, hy: str, stable_id: str, dom_fp: str):
        self.hy        = hy
        self.stable_id = stable_id
        self.dom_fp    = dom_fp
        self._u = hashlib.sha256(f"{hy}:{stable_id}".encode()).hexdigest()[:32]
        logger.info(
            f"GuardIdGenerator: 初始化完成  "
            f"stable_id={stable_id[:10]}...  u={self._u[:8]}...  dom_fp={dom_fp[:8]}..."
        )

    def generate(self, path: str, method: str) -> str:
        tb = int(time.time() * 1000) // 60000
        c  = hashlib.sha256(
            f"{path}:{method.upper()}:{self._u}:{tb}:{self.hy}".encode()
        ).hexdigest()
        buf = bytearray()
        buf.append(1)
        buf.extend(bytes.fromhex(self._u))
        buf.extend(struct.pack(">I", tb))
        buf.extend(bytes.fromhex(c))
        buf.extend(bytes.fromhex(self.dom_fp))
        return base64.urlsafe_b64encode(bytes(buf)).rstrip(b"=").decode()


# ============================================================
# 任务注册表
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
# Init Scripts
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
            }
            var wid = _origRender(container, params);
            window.__ts_wid = wid; return wid;
        };
        var _origGR = window.turnstile.getResponse.bind(window.turnstile);
        window.turnstile.getResponse = function(id) {
            if (window.__ts_injected_token) return window.__ts_injected_token;
            return _origGR(id);
        };
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

_GUARD_CAPTURE_SCRIPT = """
(function() {
    window.__guard_hy_raw = null;
    window.__guard_dom_fp = null;

    function _extractDomFp(gid) {
        try {
            if (!gid || gid.length < 80) return;
            var b64 = gid.replace(/-/g,'+').replace(/_/g,'/');
            while (b64.length % 4) b64 += '=';
            var raw = atob(b64);
            if (raw.length < 85) return;
            var hex = [];
            for (var i = 53; i < 85; i++) {
                var n = raw.charCodeAt(i).toString(16);
                hex.push(n.length < 2 ? '0'+n : n);
            }
            window.__guard_dom_fp = hex.join('');
            console.log('[GUARD] domFp captured: ' + window.__guard_dom_fp.slice(0,12) + '...');
        } catch(e) {}
    }

    if (window.crypto && window.crypto.subtle) {
        var _origDigest = window.crypto.subtle.digest.bind(window.crypto.subtle);
        window.crypto.subtle.digest = async function(algo, data) {
            var result = await _origDigest(algo, data);
            if (window.__guard_hy_raw === null) {
                try {
                    var text = new TextDecoder('utf-8', {fatal: false}).decode(data);
                    if (text.indexOf('ainnate-antibot-key-v1-') !== -1) {
                        window.__guard_hy_raw = text;
                        console.log('[GUARD] hY captured len=' + text.length);
                    }
                } catch(e) {}
            }
            return result;
        };
    }

    var _origSetReqHdr = XMLHttpRequest.prototype.setRequestHeader;
    XMLHttpRequest.prototype.setRequestHeader = function(name, value) {
        if (window.__guard_dom_fp === null) {
            if (typeof name === 'string' && name.toLowerCase() === 'x-guard-id') {
                _extractDomFp(value);
            }
        }
        return _origSetReqHdr.call(this, name, value);
    };

    var _origFetch = window.fetch;
    window.fetch = async function(url, opts) {
        if (window.__guard_dom_fp === null && opts) {
            var h = opts.headers || {};
            _extractDomFp(h['x-guard-id'] || h['X-Guard-Id'] || '');
        }
        return _origFetch.apply(this, arguments);
    };

    console.log('[GUARD] XHR + fetch + digest interceptors installed');
})();
"""


# ============================================================
# 浏览器辅助函数
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
        logger.error(f"  打开登录页失败: {e}"); return False

    rsleep(2.0, 3.0); _close_popup(page)

    email_filled = False
    for sel in ['input[name="username"]', 'input[name="email"]',
                'input[type="email"]', 'input[placeholder*="email" i]']:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=2000):
                el.fill(USERNAME); rsleep(0.5, 1.0)
                email_filled = True; break
        except: continue
    if not email_filled:
        logger.error("  找不到邮箱输入框"); return False

    pwd_filled = False
    for sel in ['input[name="password"]', 'input[type="password"]']:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=2000):
                el.fill(PASSWORD); rsleep(0.8, 1.5)
                pwd_filled = True; break
        except: continue
    if not pwd_filled:
        logger.error("  找不到密码输入框"); return False

    logger.info("  解 Turnstile（CapSolver）...")
    try:
        ts_token = _solve_turnstile()
        logger.info(f"  Turnstile token（{len(ts_token)}字符）")
    except Exception as e:
        logger.error(f"  Turnstile 失败: {e}"); return False

    try:
        ok = page.evaluate(f"window.__inject_ts('{ts_token}')")
        logger.info(f"  注入结果: callback已调用={ok}")
    except Exception as e:
        logger.warning(f"  注入异常: {e}")
    rsleep(1.5, 2.5)

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
            submitted = True
        except: pass
    if not submitted:
        logger.error("  无法提交表单"); return False

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
                            logger.error(f"  登录错误: {txt[:80]}"); return False
                except: pass
            if "geminigen.ai" in cur_url and "/auth" not in cur_url:
                rsleep(2.0, 3.0)
                if _is_logged_in(page):
                    logger.info(f"  登录成功 (URL={cur_url[:60]})"); return True
        except Exception as e:
            if _is_browser_dead_error(e): raise
            logger.warning(f"  等待时异常: {e}")
    logger.error("  登录超时"); return False

def _extract_token_from_browser(page):
    for attempt in range(6):
        try:
            data = page.evaluate("""
                (() => {
                    var token='', gid=localStorage.getItem('guard_stable_id')||'';
                    var auth=localStorage.getItem('authStore');
                    if(auth){ try{ token=JSON.parse(auth).access_token||''; }catch(e){} }
                    return { access_token: token, guard_stable_id: gid };
                })()
            """)
            token = data.get("access_token", "")
            gid   = data.get("guard_stable_id", "")
            if token: return token, gid
            logger.warning(f"  提取第{attempt+1}次: token 为空")
            rsleep(3.0, 5.0)
        except Exception as e:
            logger.error(f"  提取 token 异常: {e}"); rsleep(2.0, 3.0)
    return None, None


# ============================================================
# TokenManager
# ============================================================
class _TokenManager(threading.Thread):

    def __init__(self):
        super().__init__(name="TokenManager", daemon=True)

        self._token      = None
        self._stable_id  = None
        self._token_time = 0.0
        self._state_lock = threading.Lock()
        self._bad_tokens: set = set()
        self._refreshing = False

        self._token_ready     = threading.Event()
        self._refresh_request = threading.Event()
        self._stop_event      = threading.Event()

        self._pw:      Playwright     = None
        self._context: BrowserContext = None
        self._page:    Page           = None

        self._guard_gen: GuardIdGenerator = None
        self._guard_trigger_at: float = 0.0
        self._guard_triggered:  bool  = False

        self._gen_queue       = []
        self._gen_queue_lock  = threading.Lock()
        self._gen_queue_ready = threading.Event()

        self._video_queue      = []
        self._video_queue_lock = threading.Lock()

    # ── 公共接口 ──────────────────────────────────────────────

    def get_token(self, force_refresh=False, stale_token=None, timeout=300):
        with self._state_lock:
            if force_refresh and stale_token:
                self._bad_tokens.add(stale_token)
                if self._token and self._token not in self._bad_tokens:
                    return self._token, self._stable_id
                if not self._refreshing:
                    self._token_ready.clear()
                    self._refresh_request.set()
                    logger.info("TokenManager: 收到 token 失效通知，后台刷新已触发")
                return None, None

            if (self._token and
                    self._token not in self._bad_tokens and
                    time.time() - self._token_time < TOKEN_CACHE_TTL):
                return self._token, self._stable_id

            if not self._refreshing:
                self._token_ready.clear()
                self._refresh_request.set()

        if self._token_ready.wait(timeout=timeout):
            with self._state_lock:
                if self._token and self._token not in self._bad_tokens:
                    return self._token, self._stable_id
        logger.error(f"TokenManager.get_token: 等待超时({timeout}s)")
        return None, None

    def invalidate(self, bad_token=None):
        with self._state_lock:
            if bad_token: self._bad_tokens.add(bad_token)
            self._token      = None
            self._token_time = 0.0
            if not self._refreshing:
                self._token_ready.clear()
                self._refresh_request.set()

    def get_guard_id(self, path: str, method: str) -> str:
        if self._guard_gen is not None:
            try:
                return self._guard_gen.generate(path, method)
            except Exception as e:
                logger.warning(f"GuardIdGenerator 计算失败: {e}")
        return ""

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

        self._do_refresh(is_first=True)

        last_heartbeat = time.time()
        while not self._stop_event.is_set():
            self._gen_queue_ready.wait(timeout=1.0)
            self._gen_queue_ready.clear()
            if self._stop_event.is_set(): break

            while True:
                item = None
                with self._gen_queue_lock:
                    if self._gen_queue:
                        item = self._gen_queue.pop(0)
                if item is None: break
                self._execute_browser_gen(item)

            while True:
                item = None
                with self._video_queue_lock:
                    if self._video_queue:
                        item = self._video_queue.pop(0)
                if item is None: break
                self._execute_browser_video(item)

            if self._refresh_request.is_set():
                self._refresh_request.clear()
                self._do_refresh()

            if self._guard_gen is None:
                now = time.time()
                if not self._guard_triggered and now >= self._guard_trigger_at > 0:
                    self._guard_triggered = True
                    self._trigger_spa_request()
                self._try_init_guard_gen()

            if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL:
                self._heartbeat()
                last_heartbeat = time.time()

        self._cleanup_browser()
        logger.info("TokenManager: 线程已退出")

    # ── Token 刷新 ────────────────────────────────────────────

    def _do_refresh(self, is_first=False):
        with self._state_lock:
            if self._refreshing:
                return
            self._refreshing  = True
            self._token_ready.clear()
            bad_snapshot = set(self._bad_tokens)

        logger.info(f"TokenManager: 开始刷新 Token（首次={is_first}）...")
        try:
            for attempt in range(3):
                try:
                    if not self._is_context_alive():
                        logger.warning(f"TokenManager: 浏览器已死（第{attempt+1}次），重建...")
                        self._rebuild_context()
                        if self._context is None:
                            time.sleep(5); continue

                    page = self._page

                    if is_first:
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
                        try:
                            cur = page.url or ""
                            if "geminigen.ai" not in cur:
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
                        logger.info("TokenManager: 需要登录...")
                        try:
                            login_ok = _do_login(page)
                        except Exception as login_e:
                            if _is_browser_dead_error(login_e):
                                self._rebuild_context()
                                if self._context is None: continue
                                page = self._page
                                try: login_ok = _do_login(page)
                                except Exception: continue
                            else:
                                logger.error(f"TokenManager: 登录异常: {login_e}"); continue
                        if not login_ok:
                            logger.error("TokenManager: 登录失败"); continue

                    token, stable_id = _extract_token_from_browser(page)
                    if not token:
                        logger.warning(f"TokenManager: 提取 Token 为空（第{attempt+1}次）")
                        time.sleep(5); continue

                    if token in bad_snapshot:
                        logger.warning("TokenManager: 仍是坏 token，强制重登录...")
                        try: page.evaluate("localStorage.removeItem('authStore')")
                        except: pass
                        try:
                            page.goto(LOGIN_URL, timeout=60000)
                            page.wait_for_load_state("domcontentloaded")
                            rsleep(2.0, 3.0)
                            login_ok = _do_login(page)
                        except Exception as e:
                            logger.error(f"TokenManager: 强制重登录异常: {e}"); continue
                        if not login_ok:
                            logger.error("TokenManager: 强制重登录失败"); continue
                        token, stable_id = _extract_token_from_browser(page)
                        if not token or token in bad_snapshot:
                            logger.error("TokenManager: 重登录后 token 仍无效"); continue

                    logger.info(f"TokenManager: 获得新 Token: {token[:30]}...")
                    self._set_token(token, stable_id)

                    if self._guard_gen is None:
                        self._guard_triggered  = False
                        self._guard_trigger_at = time.time() + 5
                        logger.info("TokenManager: Guard 初始化已调度（5s 后触发 SPA）...")

                    return

                except Exception as e:
                    logger.error(f"TokenManager: 刷新异常（第{attempt+1}次）: {e}")
                    traceback.print_exc(); time.sleep(5)

            logger.error("TokenManager: Token 刷新连续失败3次")
        finally:
            with self._state_lock:
                self._refreshing = False
            self._token_ready.set()

    def _try_init_guard_gen(self):
        try:
            hy_raw = self._page.evaluate("window.__guard_hy_raw")
            dom_fp = self._page.evaluate("window.__guard_dom_fp")

            if not (hy_raw and dom_fp and len(dom_fp) == 64):
                return False

            colon     = hy_raw.index(':') if ':' in hy_raw else len(hy_raw)
            hy        = hy_raw[:colon]
            stable_id = hy_raw[colon+1:] if ':' in hy_raw else ""

            if not stable_id:
                stable_id = self._page.evaluate(
                    "localStorage.getItem('guard_stable_id') || ''"
                ) or ""

            self._guard_gen = GuardIdGenerator(hy=hy, stable_id=stable_id, dom_fp=dom_fp)
            self._verify_guard_gen()
            if self._guard_gen is not None:
                logger.info("TokenManager: GuardIdGenerator 就绪，后续提交走纯 Python 路径")
                return True
        except Exception as e:
            logger.debug(f"_try_init_guard_gen: {e}")
        return False

    def _trigger_spa_request(self):
        logger.info("TokenManager: 主动触发 SPA API 请求...")
        try:
            self._page.evaluate("""
                (async () => {
                    try {
                        const el  = document.getElementById('__nuxt');
                        const app = el && el.__vue_app__;
                        if (app && app.config.globalProperties.$router) {
                            const router = app.config.globalProperties.$router;
                            await router.push('/app/imagen?_t=' + Date.now());
                        }
                    } catch(e) {}
                    try {
                        document.dispatchEvent(new Event('visibilitychange'));
                        window.dispatchEvent(new Event('focus'));
                    } catch(e) {}
                    try {
                        const el  = document.getElementById('__nuxt');
                        const app = el && el.__vue_app__;
                        if (app) {
                            const pinia = app._context.provides.pinia;
                            if (pinia && pinia._s) {
                                for (const [id, store] of pinia._s.entries()) {
                                    for (const key of Object.keys(store)) {
                                        if (typeof store[key] === 'function' &&
                                            /^(fetch|load|init|refresh|get)/i.test(key)) {
                                            try { await store[key](); return; } catch(e2) {}
                                        }
                                    }
                                }
                            }
                        }
                    } catch(e) {}
                })()
            """)
        except Exception as e:
            logger.debug(f"_trigger_spa_request: {e}")

    def _verify_guard_gen(self):
        try:
            gid = self._guard_gen.generate("/api/test", "get")
            raw = base64.urlsafe_b64decode(gid + "====")
            assert len(raw) == 85, f"长度错误: {len(raw)}"
            assert raw[0] == 1, f"version byte 错误: {raw[0]}"
            logger.info(f"TokenManager: GuardIdGenerator 验证通过  sample={gid[:20]}...")
        except Exception as e:
            logger.error(f"TokenManager: GuardIdGenerator 验证失败: {e}")
            self._guard_gen = None

    def _heartbeat(self):
        if self._guard_gen is None:
            logger.debug("TokenManager: 心跳跳过（GuardIdGenerator 未就绪）")
            return

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

        if token_age > TOKEN_CACHE_TTL - TOKEN_PREREFRESH:
            logger.info(f"TokenManager: Token 即将过期（已用{token_age:.0f}s），主动刷新...")
            with self._state_lock:
                if not self._refreshing:
                    self._token_ready.clear()
                    self._refresh_request.set()
            return

        try:
            guard_id = self.get_guard_id("/api/histories", "get")
            headers  = _build_headers(token, guard_id)
            resp = req_lib.get(
                f"{API_BASE}/histories?items_per_page=1&page=1",
                headers=headers, timeout=15,
                proxies={"http": None, "https": None}
            )
            if resp.status_code in (401, 403):
                logger.warning(f"TokenManager: 心跳 HTTP {resp.status_code}，触发刷新...")
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

    def _set_token(self, token, stable_id):
        with self._state_lock:
            self._token      = token
            self._stable_id  = stable_id
            self._token_time = time.time()
            self._bad_tokens.clear()
        self._token_ready.set()

    # ── 提交队列 ──────────────────────────────────────────────

    def submit_generate(self, prompt_text, aspect_ratio, resolution, model=None,
                        timeout=300, reference_images=None):
        """Worker线程调用：排队等待提交生成任务"""
        if model is None:
            model = MODEL_PRIMARY
        event      = threading.Event()
        result_box = [None]
        with self._gen_queue_lock:
            self._gen_queue.append((event, result_box, prompt_text, aspect_ratio,
                                    resolution, model, reference_images))
            qlen = len(self._gen_queue)
        self._gen_queue_ready.set()
        if qlen > 1:
            logger.debug(f"TokenManager: 提交队列积压 {qlen} 项")
        if event.wait(timeout=timeout):
            return result_box[0] or (0, None, "empty_result")
        logger.error(f"TokenManager.submit_generate: 超时 ({timeout}s)")
        return (0, None, "browser_timeout")

    def _execute_browser_gen(self, item):
        event, result_box, prompt_text, aspect_ratio, resolution, model, reference_images = item
        try:
            result_box[0] = self._do_generate(prompt_text, aspect_ratio, resolution, model,
                                              reference_images=reference_images)
        except Exception as e:
            logger.error(f"TokenManager._execute_browser_gen 异常: {e}")
            result_box[0] = (0, None, "browser_exception")
        finally:
            event.set()

    def submit_video_browser(self, model, prompt_text, aspect_ratio, resolution,
                             duration, enhance_prompt, mode_image,
                             ref_image_path=None, timeout=600):
        """Worker线程调用：用浏览器 fetch 提交视频生成（GuardIdGenerator 未就绪时降级）"""
        event      = threading.Event()
        result_box = [None]
        with self._video_queue_lock:
            self._video_queue.append((event, result_box, model, prompt_text,
                                      aspect_ratio, resolution, duration,
                                      enhance_prompt, mode_image, ref_image_path))
        self._gen_queue_ready.set()
        if event.wait(timeout=timeout):
            return result_box[0] or (0, None, "empty_result")
        logger.error(f"TokenManager.submit_video_browser: 超时 ({timeout}s)")
        return (0, None, "browser_timeout")

    def _execute_browser_video(self, item):
        (event, result_box, model, prompt_text, aspect_ratio, resolution,
         duration, enhance_prompt, mode_image, ref_image_path) = item
        try:
            with self._state_lock:
                token = self._token
            if not token:
                result_box[0] = (0, None, "no_token")
                return
            ts_token = _get_turnstile_token()
            result_box[0] = self._do_browser_fetch_video(
                token, model, prompt_text, aspect_ratio, resolution,
                duration, enhance_prompt, mode_image, ts_token, ref_image_path,
            )
        except Exception as e:
            logger.error(f"TokenManager._execute_browser_video 异常: {e}")
            result_box[0] = (0, None, "browser_exception")
        finally:
            event.set()

    def _do_browser_fetch_video(self, token, model, prompt_text, aspect_ratio,
                                resolution, duration, enhance_prompt, mode_image,
                                ts_token="skip", ref_image_path=None):
        """在浏览器内用 ab() 生成 x-guard-id，提交视频生成请求"""
        import base64 as _b64
        ref_image_data = None
        if ref_image_path and os.path.exists(ref_image_path):
            try:
                with open(ref_image_path, "rb") as fh:
                    raw = fh.read()
                ext  = os.path.splitext(ref_image_path)[1].lower()
                mime = ("image/png" if ext == ".png"
                        else "image/webp" if ext == ".webp"
                        else "image/jpeg")
                ref_image_data = {
                    "name": os.path.basename(ref_image_path),
                    "b64":  _b64.b64encode(raw).decode("ascii"),
                    "mime": mime,
                }
            except Exception as e:
                logger.warning(f"  [Browser-Video] 读取参考图失败: {e}")

        if model == "grok-video":
            api_url  = f"{API_BASE}/video-gen/grok-stream"
            api_path = "/api/video-gen/grok-stream"
            _ar_map  = {"16:9": "landscape", "9:16": "portrait", "1:1": "square"}
            mapped_ar = _ar_map.get(aspect_ratio, aspect_ratio)
        else:
            api_url   = f"{API_BASE}/video-gen/veo"
            api_path  = "/api/video-gen/veo"
            mapped_ar = aspect_ratio

        try:
            result = self._page.evaluate("""
                async function(args) {
                    const {token, model, prompt, aspectRatio, resolution, duration,
                           enhancePrompt, modeImage, tsToken, apiUrl, apiPath, refImage} = args;

                    async function G3(str) {
                        const data = new TextEncoder().encode(str);
                        const buf  = await crypto.subtle.digest('SHA-256', data);
                        return Array.from(new Uint8Array(buf))
                            .map(b => b.toString(16).padStart(2,'0')).join('');
                    }
                    function bm(hex) {
                        const arr = [];
                        for (let i = 0; i < hex.length; i += 2)
                            arr.push(parseInt(hex.substr(i, 2), 16));
                        return arr;
                    }
                    function lY(n) {
                        return [(n>>>24)&255, (n>>>16)&255, (n>>>8)&255, n&255];
                    }
                    function IY(bytes) {
                        return btoa(String.fromCharCode(...bytes))
                            .replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=+$/,'');
                    }
                    async function fY() {
                        const KEY   = 'guard_stable_id';
                        const VALID = /^[A-Za-z0-9_-]{22}$/;
                        try {
                            const cached = localStorage.getItem(KEY);
                            if (cached && VALID.test(cached)) return cached;
                        } catch(e) {}
                        const rand  = Array.from(crypto.getRandomValues(new Uint8Array(16)))
                                          .map(b=>b.toString(16).padStart(2,'0')).join('');
                        const ua    = navigator.userAgent || 'unknown';
                        const sc    = (screen.width||0) + 'x' + (screen.height||0);
                        const hash  = await G3('default:' + rand + ':' + ua + ':' + sc);
                        const newId = IY(bm(hash)).slice(0, 22);
                        try { localStorage.setItem(KEY, newId); } catch(e) {}
                        return newId;
                    }
                    async function EY() {
                        function G4(e) {
                            let i = 0;
                            const t = String(e);
                            for (let s = 0; s < t.length; s++) i = i*31 + t.charCodeAt(s) | 0;
                            return i >>> 0;
                        }
                        const parts = [
                            navigator.userAgent,
                            screen.width + 'x' + screen.height,
                            screen.colorDepth,
                            navigator.language,
                            navigator.hardwareConcurrency || 0,
                            Intl.DateTimeFormat().resolvedOptions().timeZone || '',
                        ];
                        let combined = parts.map(p => {
                            const l = G4(String(p)).toString(2);
                            return G4(l).toString(16);
                        }).join('').replace(/[.-]/g,'');
                        return await G3(combined);
                    }
                    async function ab(path, method) {
                        const stableId   = await fY();
                        const timeBucket = Math.floor(Date.now() / 60000);
                        const domFp      = await EY();
                        let secretKey = '';
                        try {
                            const el  = document.getElementById('__nuxt');
                            const app = el && el.__vue_app__;
                            if (app) {
                                const cfg = app.config.globalProperties.$config;
                                secretKey = (cfg && cfg.public && cfg.public.antibot &&
                                             cfg.public.antibot.secretKey) || '';
                            }
                        } catch(e) {}
                        const u  = (await G3(secretKey + ':' + stableId)).slice(0, 32);
                        const c  = await G3(path + ':' + method.toUpperCase()
                                           + ':' + u + ':' + timeBucket + ':' + secretKey);
                        const buf = new Uint8Array(85);
                        buf[0] = 1;
                        bm(u).forEach((v,i)  => buf[i+1]  = v);
                        lY(timeBucket).forEach((v,i) => buf[i+17] = v);
                        bm(c).forEach((v,i)  => buf[i+21] = v);
                        bm(domFp).forEach((v,i) => buf[i+53] = v);
                        return IY(Array.from(buf));
                    }

                    const fd = new FormData();
                    fd.append('prompt',          prompt);
                    fd.append('model',           model);
                    fd.append('aspect_ratio',    aspectRatio);
                    fd.append('turnstile_token', tsToken);

                    if (model === 'grok-video') {
                        fd.append('resolution', resolution || '480p');
                        fd.append('duration',   String(duration || 6));
                        fd.append('mode',       'custom');
                    } else {
                        fd.append('enhance_prompt', enhancePrompt ? 'true' : 'false');
                        fd.append('duration',       String(duration || 8));
                        fd.append('resolution',     resolution || '1080p');
                        fd.append('mode_image',     modeImage || 'ingredient');
                        if (refImage) {
                            const bytes = Uint8Array.from(atob(refImage.b64), c => c.charCodeAt(0));
                            const blob  = new Blob([bytes], {type: refImage.mime});
                            const file  = new File([blob], refImage.name, {type: refImage.mime});
                            fd.append('ref_images', file, refImage.name);
                        } else {
                            fd.append('ref_images', '');
                        }
                    }

                    try {
                        const guardId = await ab(apiPath, 'post');
                        const r = await fetch(apiUrl, {
                            method:  'POST',
                            headers: {
                                'authorization': 'Bearer ' + token,
                                'x-guard-id':    guardId,
                            },
                            body: fd,
                        });
                        return {status: r.status, body: await r.text()};
                    } catch(e) {
                        return {status: 0, error: String(e)};
                    }
                }
            """, {
                "token": token, "model": model, "prompt": prompt_text,
                "aspectRatio": mapped_ar, "resolution": resolution,
                "duration": str(duration or 8),
                "enhancePrompt": enhance_prompt, "modeImage": mode_image or "ingredient",
                "tsToken": ts_token, "apiUrl": api_url, "apiPath": api_path,
                "refImage": ref_image_data,
            })
        except Exception as e:
            logger.error(f"  [Browser-Video] page.evaluate 异常: {e}")
            return (0, None, "evaluate_error")

        status = result.get("status", 0)
        body   = result.get("body", "")
        logger.info(f"  [Browser-Video] 提交响应 HTTP {status}")

        if status == 200:
            try:
                data = json.loads(body)
                history_id = (data.get("uuid") or data.get("history_id")
                              or data.get("id") or data.get("task_id"))
                if not history_id:
                    uuids = re.findall(
                        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", body)
                    history_id = uuids[0] if uuids else None
                logger.info(f"  [Browser-Video] history_id={history_id}")
                return (200, history_id, "")
            except Exception as e:
                logger.error(f"  [Browser-Video] 解析响应失败: {e}  body={body[:200]}")
                return (200, None, "parse_error")
        elif status == 429:
            _trigger_rate_limit()
            return (429, None, "RATE_LIMIT")
        elif status in (401, 403):
            return (status, None, "TOKEN_EXPIRED")
        elif status == 400:
            try:
                detail = json.loads(body).get("detail", {})
                ec = (detail.get("error_code") if isinstance(detail, dict) else "") or ""
                if ec == "TURNSTILE_INVALID":
                    return (400, None, "TURNSTILE_INVALID")
            except Exception:
                pass
            logger.error(f"  [Browser-Video] HTTP 400: {body[:200]}")
            return (400, None, "")
        else:
            logger.error(f"  [Browser-Video] HTTP {status}: {body[:100]}")
            return (status, None, "")

    def _do_generate(self, prompt_text, aspect_ratio, resolution, model=MODEL_PRIMARY,
                     reference_images=None):
        """
        提交 generate_image 请求。
        方案A：Python requests + Python 生成的 guard-id（GuardIdGenerator 就绪时）
        方案B：浏览器 fetch 降级（guard-id 由浏览器 Axios 拦截器自动生成）
        Turnstile 自适应：默认 skip，被拒后切换 CapSolver
        """
        with self._state_lock:
            token = self._token
        if not token:
            return (0, None, "no_token")

        try:
            ts_token = _get_turnstile_token()
        except Exception as e:
            logger.error(f"  CapSolver 获取失败，本次跳过提交: {e}")
            return (0, None, "turnstile_capsolver_failed")

        logger.info(f"  [Model] 当前使用: {model}")

        # ── 方案A：Python HTTP ──
        if self._guard_gen is not None:
            guard_id = self._guard_gen.generate("/api/generate_image", "post")
            logger.info(f"  [Python] 提交 generate_image  ts={ts_token[:8]}  guard_id={guard_id[:20]}...")
            status_code, task_uuid, err = _api_generate_http(
                token, guard_id, ts_token, prompt_text, aspect_ratio, resolution, model,
                reference_images=reference_images
            )
            if err == "TURNSTILE_INVALID":
                _turnstile_on_skip_fail()
                logger.info("  [Turnstile] skip 被拒，立即用 CapSolver 重试...")
                try:
                    ts_token = _solve_turnstile()
                except Exception as e:
                    logger.error(f"  CapSolver 重试获取失败: {e}")
                    return (status_code, None, "turnstile_capsolver_failed")
                guard_id = self._guard_gen.generate("/api/generate_image", "post")
                status_code, task_uuid, err = _api_generate_http(
                    token, guard_id, ts_token, prompt_text, aspect_ratio, resolution, model,
                    reference_images=reference_images
                )
            if err == "" and task_uuid:
                _turnstile_on_success()
            return (status_code, task_uuid, err)

        # ── 方案B：浏览器 fetch 降级 ──
        logger.info("  [Browser] GuardIdGenerator 未就绪，降级使用浏览器 fetch...")
        if not self._is_context_alive():
            self._rebuild_context()
            if not self._is_context_alive():
                return (0, None, "browser_dead")
        status_code, task_uuid, err = self._do_browser_fetch(
            token, prompt_text, aspect_ratio, resolution, ts_token, model,
            reference_images=reference_images
        )
        if err == "TURNSTILE_INVALID":
            _turnstile_on_skip_fail()
            logger.info("  [Turnstile][Browser] skip 被拒，立即用 CapSolver 重试...")
            try:
                ts_token = _solve_turnstile()
            except Exception as e:
                logger.error(f"  CapSolver 重试获取失败: {e}")
                return (status_code, None, "turnstile_capsolver_failed")
            status_code, task_uuid, err = self._do_browser_fetch(
                token, prompt_text, aspect_ratio, resolution, ts_token, model,
                reference_images=reference_images
            )
        if err == "" and task_uuid:
            _turnstile_on_success()
        return (status_code, task_uuid, err)

    def _do_browser_fetch(self, token, prompt_text, aspect_ratio, resolution,
                          ts_token="skip", model=MODEL_PRIMARY, reference_images=None):
        """在浏览器内执行 ab() 生成 x-guard-id，然后用 fetch 提交（含参考图）"""
        import base64

        encoded_images = []
        if reference_images:
            for img_path in reference_images[:5]:
                if img_path and os.path.exists(img_path):
                    try:
                        with open(img_path, "rb") as fh:
                            raw = fh.read()
                        ext  = os.path.splitext(img_path)[1].lower()
                        mime = ("image/png" if ext == ".png"
                                else "image/webp" if ext == ".webp"
                                else "image/jpeg")
                        encoded_images.append({
                            "name": os.path.basename(img_path),
                            "b64":  base64.b64encode(raw).decode("ascii"),
                            "mime": mime,
                        })
                    except Exception as e:
                        logger.warning(f"  [Browser] 读取参考图失败 {img_path}: {e}")

        try:
            result = self._page.evaluate("""
                async function(args) {
                    const {token, prompt, aspectRatio, resolution, tsToken, model, refImages} = args;

                    async function G3(str) {
                        const data = new TextEncoder().encode(str);
                        const buf  = await crypto.subtle.digest('SHA-256', data);
                        return Array.from(new Uint8Array(buf))
                            .map(b => b.toString(16).padStart(2,'0')).join('');
                    }
                    function bm(hex) {
                        const arr = [];
                        for (let i = 0; i < hex.length; i += 2)
                            arr.push(parseInt(hex.substr(i, 2), 16));
                        return arr;
                    }
                    function lY(n) {
                        return [(n>>>24)&255, (n>>>16)&255, (n>>>8)&255, n&255];
                    }
                    function IY(bytes) {
                        return btoa(String.fromCharCode(...bytes))
                            .replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=+$/,'');
                    }
                    async function fY() {
                        const KEY   = 'guard_stable_id';
                        const VALID = /^[A-Za-z0-9_-]{22}$/;
                        try {
                            const cached = localStorage.getItem(KEY);
                            if (cached && VALID.test(cached)) return cached;
                        } catch(e) {}
                        const rand  = Array.from(crypto.getRandomValues(new Uint8Array(16)))
                                          .map(b=>b.toString(16).padStart(2,'0')).join('');
                        const ua    = navigator.userAgent || 'unknown';
                        const sc    = (screen.width||0) + 'x' + (screen.height||0);
                        const hash  = await G3('default:' + rand + ':' + ua + ':' + sc);
                        const newId = IY(bm(hash)).slice(0, 22);
                        try { localStorage.setItem(KEY, newId); } catch(e) {}
                        return newId;
                    }
                    async function EY() {
                        function G4(e) {
                            let i = 0;
                            const t = String(e);
                            for (let s = 0; s < t.length; s++) i = i*31 + t.charCodeAt(s) | 0;
                            return i >>> 0;
                        }
                        const parts = [
                            navigator.userAgent,
                            screen.width + 'x' + screen.height,
                            screen.colorDepth,
                            navigator.language,
                            navigator.hardwareConcurrency || 0,
                            Intl.DateTimeFormat().resolvedOptions().timeZone || '',
                        ];
                        let combined = parts.map(p => {
                            const l = G4(String(p)).toString(2);
                            return G4(l).toString(16);
                        }).join('').replace(/[.-]/g,'');
                        return await G3(combined);
                    }
                    async function ab(path, method) {
                        const stableId   = await fY();
                        const timeBucket = Math.floor(Date.now() / 60000);
                        const domFp      = await EY();
                        let secretKey = '';
                        try {
                            const el  = document.getElementById('__nuxt');
                            const app = el && el.__vue_app__;
                            if (app) {
                                const cfg = app.config.globalProperties.$config;
                                secretKey = (cfg && cfg.public && cfg.public.antibot &&
                                             cfg.public.antibot.secretKey) || '';
                            }
                        } catch(e) {}
                        const u  = (await G3(secretKey + ':' + stableId)).slice(0, 32);
                        const c  = await G3(path + ':' + method.toUpperCase()
                                           + ':' + u + ':' + timeBucket + ':' + secretKey);
                        const buf = new Uint8Array(85);
                        buf[0] = 1;
                        bm(u).forEach((v,i)  => buf[i+1]  = v);
                        lY(timeBucket).forEach((v,i) => buf[i+17] = v);
                        bm(c).forEach((v,i)  => buf[i+21] = v);
                        bm(domFp).forEach((v,i) => buf[i+53] = v);
                        return IY(Array.from(buf));
                    }

                    const fd = new FormData();
                    fd.append('prompt',          prompt);
                    fd.append('model',           model);
                    fd.append('aspect_ratio',    aspectRatio);
                    fd.append('output_format',   'png');
                    fd.append('resolution',      resolution);
                    fd.append('turnstile_token', tsToken);

                    if (refImages && refImages.length > 0) {
                        for (const img of refImages) {
                            const bytes = Uint8Array.from(atob(img.b64), c => c.charCodeAt(0));
                            const blob  = new Blob([bytes], {type: img.mime});
                            const file  = new File([blob], img.name, {type: img.mime});
                            fd.append('files', file, img.name);
                        }
                    }

                    try {
                        const guardId = await ab('/api/generate_image', 'post');
                        const r = await fetch('https://api.geminigen.ai/api/generate_image', {
                            method: 'POST',
                            headers: {
                                'authorization': 'Bearer ' + token,
                                'x-guard-id':    guardId,
                            },
                            body: fd,
                        });
                        return {status: r.status, body: await r.text()};
                    } catch(e) {
                        return {status: 0, error: String(e)};
                    }
                }
            """, {
                "token": token, "prompt": prompt_text,
                "aspectRatio": aspect_ratio, "resolution": resolution,
                "tsToken": ts_token, "model": model,
                "refImages": encoded_images,
            })
        except Exception as e:
            logger.error(f"  [Browser] page.evaluate 异常: {e}")
            return (0, None, "evaluate_error")

        return self._parse_generate_response(result.get("status", 0), result.get("body", ""))

    @staticmethod
    def _parse_generate_response(status, body):
        if status == 200:
            try:
                data      = json.loads(body)
                task_uuid = data.get("uuid") or data.get("id")
                if not task_uuid:
                    uuids = re.findall(
                        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                        body
                    )
                    task_uuid = uuids[0] if uuids else None
                if task_uuid:
                    logger.info(f"  UUID: {task_uuid}")
                return (200, task_uuid, "")
            except Exception as e:
                logger.error(f"  解析响应失败: {e} body={body[:200]}")
                return (200, None, "")
        elif status == 429:
            _trigger_rate_limit()
            return (429, None, "RATE_LIMIT")
        elif status in (401, 403):
            return (status, None, "TOKEN_EXPIRED")
        elif status == 400:
            try:
                err_data   = json.loads(body)
                detail     = err_data.get("detail", {})
                error_code = (detail.get("error_code") if isinstance(detail, dict) else "") or ""
                if error_code == "TURNSTILE_INVALID":
                    logger.warning("  服务端要求真实 Turnstile token")
                    return (400, None, "TURNSTILE_INVALID")
                if error_code == "MAX_PROCESSING_IMAGEN_EXCEEDED":
                    logger.error(f"  HTTP 400: {body[:200]}")
                    return (400, None, "MAX_PROCESSING_IMAGEN_EXCEEDED")
            except: pass
            logger.error(f"  HTTP 400: {body[:200]}")
            return (400, None, "")
        else:
            error_code = ""
            try:
                err_data   = json.loads(body)
                detail     = err_data.get("detail", {})
                error_code = (detail.get("error_code") if isinstance(detail, dict) else "") or ""
                error_msg  = (detail.get("message") if isinstance(detail, dict) else str(detail)) or body
                if _is_image_format_error(error_code, error_msg):
                    return (status, None, "IMAGE_FORMAT_ERROR")
                if error_code == "TURNSTILE_INVALID":
                    return (status, None, "TURNSTILE_INVALID")
                ec_l = error_code.lower(); em_l = error_msg.lower()
                if any(k in ec_l or k in em_l for k in _RATE_LIMIT_INDICATORS):
                    _trigger_rate_limit()
                    return (status, None, "RATE_LIMIT")
                if "MAX_PROCESSING_IMAGEN_EXCEEDED" in str(detail):
                    return (status, None, "MAX_PROCESSING_IMAGEN_EXCEEDED")
            except: pass
            logger.error(f"  HTTP {status}: {body[:200]}")
            return (status, None, error_code)

    # ── 浏览器管理 ────────────────────────────────────────────

    def _create_context(self):
        profile_dir    = _get_profile_dir()
        is_new_profile = not os.path.exists(profile_dir)
        os.makedirs(profile_dir, exist_ok=True)
        if self._pw is None:
            self._pw = sync_playwright().start()
        logger.info(f"TokenManager: {'新建' if is_new_profile else '复用'}浏览器  账号={USERNAME}")
        ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=HIDE_WINDOW,
            args=[
                "--no-sandbox", "--disable-blink-features=AutomationControlled",
                "--lang=zh-CN,zh,en-US,en", "--window-size=1920,1080",
                "--disable-extensions", "--disable-translate",
            ],
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN", ignore_https_errors=True,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
            ),
        )
        ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins',   { get: () => [1,2,3,4,5] });
            window.chrome = { runtime: {} };
        """)
        ctx.add_init_script(_GUARD_CAPTURE_SCRIPT)
        self._context = ctx
        self._page    = ctx.pages[0] if ctx.pages else ctx.new_page()
        logger.info("TokenManager: 浏览器实例已就绪")

    def _is_context_alive(self):
        if self._context is None or self._page is None: return False
        try: _ = self._page.url; return True
        except: return False

    def _rebuild_context(self):
        logger.warning("TokenManager: 浏览器连接断开，正在重建...")
        for obj in [self._page, self._context]:
            if obj:
                try: obj.close()
                except: pass
        self._page = self._context = None
        with self._state_lock:
            self._token = None; self._stable_id = None; self._token_time = 0.0
        self._guard_gen        = None
        self._guard_triggered  = False
        self._guard_trigger_at = 0.0
        try:
            self._create_context()
            logger.info("TokenManager: 浏览器重建成功")
        except Exception as e:
            logger.error(f"TokenManager: 浏览器重建失败: {e}")
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

def _get_fresh_token(force_refresh=False, stale_token=None):
    try:
        return _get_token_manager().get_token(
            force_refresh=force_refresh, stale_token=stale_token, timeout=300
        )
    except RuntimeError as e:
        logger.error(f"_get_fresh_token: {e}")
        return None, None

def _invalidate_token_cache(bad_token=None):
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
    logger.info("初始化：启动 TokenManager...")
    logger.info("=" * 40)
    with _token_manager_lock:
        if _token_manager is not None and _token_manager.is_alive():
            logger.info("TokenManager 已在运行，跳过重复初始化")
            return True
        _token_manager = _TokenManager()
        _token_manager.start()

    if _token_manager._token_ready.wait(timeout=300):
        token, _ = _token_manager.get_token(timeout=10)
        if token:
            logger.info("初始化完成，登录状态正常")
            return True
    logger.error("初始化失败：TokenManager 启动超时或登录失败")
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
        try:
            return req_lib.get(url, headers=headers, timeout=timeout,
                               proxies={"http": None, "https": None})
        except (ReqConnectionError, Timeout, ConnectionError, OSError) as e:
            if a < NET_RETRY_COUNT:
                logger.warning(f"  网络错误(GET第{a}次): {type(e).__name__}, {NET_RETRY_DELAY}s后重试...")
                time.sleep(NET_RETRY_DELAY)
            else: raise

def _build_headers(token, guard_id=""):
    h = {
        "accept":             "*/*",
        "accept-language":    "zh-CN,zh;q=0.9",
        "authorization":      f"Bearer {token}",
        "origin":             "https://geminigen.ai",
        "referer":            "https://geminigen.ai/",
        "sec-ch-ua":          '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
        "sec-ch-ua-mobile":   "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest":     "empty",
        "sec-fetch-mode":     "cors",
        "sec-fetch-site":     "same-site",
        "user-agent":         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    }
    if guard_id:
        h["x-guard-id"] = guard_id
    return h


# ============================================================
# CapSolver
# ============================================================
def _solve_turnstile():
    def _post_json(url, payload):
        for a in range(1, NET_RETRY_COUNT + 1):
            try:
                return req_lib.post(url, json=payload, timeout=30,
                                    proxies={"http": None, "https": None})
            except (ReqConnectionError, Timeout, ConnectionError, OSError) as e:
                if a < NET_RETRY_COUNT:
                    time.sleep(NET_RETRY_DELAY)
                else:
                    raise

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
            resp   = _post_json("https://api.capsolver.com/createTask", payload)
            result = resp.json()
            if result.get("errorId", 0) != 0:
                raise RuntimeError(f"创建失败: {result.get('errorDescription')}")
            task_id = result["taskId"]
            for i in range(60):
                time.sleep(3)
                resp = _post_json(
                    "https://api.capsolver.com/getTaskResult",
                    {"clientKey": CAPSOLVER_API_KEY, "taskId": task_id}
                )
                r = resp.json()
                if r.get("status") == "ready":
                    token = r["solution"]["token"]
                    logger.info(f"  Turnstile 已解决 ({len(token)}字符)"); return token
                elif r.get("status") == "failed":
                    raise RuntimeError(f"失败: {r.get('errorDescription', r.get('errorCode'))}")
                elif r.get("status") == "processing":
                    if i % 5 == 0: logger.info(f"  等待 CapSolver... ({i*3}s)")
                else:
                    raise RuntimeError(f"异常状态: {r.get('status')}")
            raise RuntimeError("超时")
        except Exception as e:
            logger.warning(f"  CapSolver 第{attempt}次失败: {e}")
            if attempt < CAPSOLVER_MAX_RETRY: time.sleep(random.uniform(3.0, 5.0))
            else: raise


# ============================================================
# Python HTTP 提交（GuardIdGenerator 就绪时使用）
# ============================================================
def _api_generate_http(token, guard_id, turnstile_token, prompt_text,
                       aspect_ratio="1:1", resolution="1K", model=MODEL_PRIMARY,
                       reference_images=None):
    url     = f"{API_BASE}/generate_image"
    headers = _build_headers(token, guard_id)
    n_imgs  = len(reference_images) if reference_images else 0
    logger.info(f"  [Python] ratio={aspect_ratio}  res={resolution}  model={model}  imgs={n_imgs}  guard={guard_id[:20]}...")

    data = {
        "prompt":          prompt_text,
        "model":           model,
        "aspect_ratio":    aspect_ratio,
        "output_format":   "png",
        "resolution":      resolution,
        "turnstile_token": turnstile_token,
    }

    open_handles = []
    files = None
    if reference_images:
        files = []
        for img_path in reference_images[:5]:
            if img_path and os.path.exists(img_path):
                try:
                    fh = open(img_path, "rb")
                    open_handles.append(fh)
                    ext  = os.path.splitext(img_path)[1].lower()
                    mime = ("image/png" if ext == ".png"
                            else "image/webp" if ext == ".webp"
                            else "image/jpeg")
                    files.append(("files", (os.path.basename(img_path), fh, mime)))
                except Exception as e:
                    logger.warning(f"  [Python] 打开参考图失败 {img_path}: {e}")
        if not files:
            files = None

    try:
        for a in range(1, NET_RETRY_COUNT + 1):
            try:
                resp = req_lib.post(url, headers=headers, data=data,
                                    files=files if files else None,
                                    timeout=120,
                                    proxies={"http": None, "https": None})
                break
            except (ReqConnectionError, Timeout, ConnectionError, OSError) as e:
                if a < NET_RETRY_COUNT:
                    logger.warning(f"  网络错误(第{a}次): {e}, 重试...")
                    time.sleep(NET_RETRY_DELAY)
                else:
                    raise
    finally:
        for fh in open_handles:
            try:
                fh.close()
            except Exception:
                pass

    logger.info(f"  API响应: HTTP {resp.status_code}")
    return _TokenManager._parse_generate_response(resp.status_code, resp.text)


# ============================================================
# 轮询 + 下载结果
# ============================================================
def _api_poll_and_download(task_uuid, save_path):
    event = _register_task(task_uuid)
    consecutive_net_errors = 0
    try:
        start = time.time(); got_4xx = False
        current_token = stale_token_to_report = None

        while time.time() - start < GENERATE_TIMEOUT:
            elapsed = int(time.time() - start)

            if event.is_set():
                result = _get_task_result(task_uuid)
                if result:
                    if result["status"] == 2 and result["thumbnail_url"]:
                        logger.info("  生成完成（被其他 worker 发现）!")
                        return (_download_image(result["thumbnail_url"], save_path),
                                result["thumbnail_url"], "")
                    elif result["status"] == 3:
                        ec = result.get("error_code", "") or ""
                        em = result.get("error_message", "") or ""
                        logger.error(f"  任务失败: {ec} - {em}")
                        if _is_image_format_error(ec, em): return (False, None, "IMAGE_FORMAT_ERROR")
                        return (False, None, "")

            if got_4xx:
                _get_fresh_token(force_refresh=True, stale_token=stale_token_to_report)
                got_4xx = False; stale_token_to_report = None
                logger.info("  Token 失效已通知，等待15s后重新获取...")
                time.sleep(15)
                current_token, _ = _get_fresh_token()
            else:
                current_token, _ = _get_fresh_token()

            if not current_token:
                logger.error("  无法获取 token，等待15秒后重试...")
                time.sleep(15); consecutive_net_errors += 1
                if consecutive_net_errors >= 5:
                    logger.error("  连续5次无法获取 token，放弃")
                    return (False, None, "")
                continue
            else:
                consecutive_net_errors = 0

            try:
                guard_id = _get_token_manager().get_guard_id("/api/histories", "get")
                headers  = _build_headers(current_token, guard_id)
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
                                        return (_download_image(thumb_url, save_path), thumb_url, "")
                                elif status == 3:
                                    ec = item.get("error_code", "") or ""
                                    em = item.get("error_message", "") or ""
                                    logger.error(f"  任务失败: {ec} - {em}")
                                    if _is_image_format_error(ec, em):
                                        return (False, None, "IMAGE_FORMAT_ERROR")
                                    return (False, None, "")
                                break
                        if found_task: break
                        if len(page_results) < 50: break
                    elif resp.status_code in (401, 403):
                        stale_token_to_report = current_token
                        got_4xx = True
                        logger.warning(f"  [{elapsed}s] HTTP {resp.status_code}，token 失效")
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
                    _invalidate_token_cache(bad_token=current_token)
                    time.sleep(30); consecutive_net_errors = 0
            event.wait(timeout=API_POLL_INTERVAL)

        logger.error(f"  超时 ({GENERATE_TIMEOUT}s)")
        return (False, None, "")
    finally:
        _unregister_task(task_uuid)


def _download_image(url, save_path):
    return _download_file(url, save_path)


# ============================================================
# 视频生成 HTTP 提交
# ============================================================
VIDEO_POLL_INTERVAL = 10
VIDEO_GENERATE_TIMEOUT = 300  # 5 分钟

def _api_submit_video(token, guard_id, turnstile_token, prompt_text,
                      model, aspect_ratio, resolution, duration,
                      enhance_prompt, mode_image, ref_image_path=None):
    """
    提交视频生成任务。
    Grok: POST /api/video-gen/grok-stream
    Veo:  POST /api/video-gen/veo
    返回 (status_code, history_id, error_str)
    """
    if model == "grok-video":
        url  = f"{API_BASE}/video-gen/grok-stream"
        path = "/api/video-gen/grok-stream"
        # Grok aspect_ratio 用 landscape/portrait/square
        _ar_map = {"16:9": "landscape", "9:16": "portrait", "1:1": "square"}
        grok_ar = _ar_map.get(aspect_ratio, aspect_ratio)
        data = {
            "prompt":          prompt_text,
            "model":           "grok-video",
            "aspect_ratio":    grok_ar,
            "resolution":      resolution or "480p",
            "duration":        str(duration or 6),
            "mode":            "custom",
            "turnstile_token": turnstile_token,
        }
        files = None
    else:
        url  = f"{API_BASE}/video-gen/veo"
        path = "/api/video-gen/veo"
        data = {
            "prompt":          prompt_text,
            "model":           "veo-3-fast",
            "aspect_ratio":    aspect_ratio or "16:9",
            "turnstile_token": turnstile_token,
            "enhance_prompt":  "true" if enhance_prompt else "false",
            "duration":        str(duration or 8),
            "resolution":      resolution or "1080p",
            "mode_image":      mode_image or "ingredient",
        }
        files = None
        if ref_image_path and os.path.exists(ref_image_path):
            try:
                ext  = os.path.splitext(ref_image_path)[1].lower()
                mime = "image/png" if ext == ".png" else "image/webp" if ext == ".webp" else "image/jpeg"
                files = [("ref_images", (os.path.basename(ref_image_path),
                                         open(ref_image_path, "rb"), mime))]
            except Exception as e:
                logger.warning(f"  [Video] 打开参考图失败: {e}")
        if not files:
            data["ref_images"] = ""

    guard_id = _get_token_manager().get_guard_id(path, "post")

    headers = _build_headers(token, guard_id)

    open_handles = [f[1][1] for f in (files or []) if hasattr(f[1][1], "read")]
    try:
        for attempt in range(1, NET_RETRY_COUNT + 1):
            try:
                resp = req_lib.post(
                    url, headers=headers,
                    data=data, files=files or None,
                    timeout=120,
                    proxies={"http": None, "https": None},
                )
                break
            except (ReqConnectionError, Timeout, ConnectionError, OSError) as e:
                if attempt < NET_RETRY_COUNT:
                    logger.warning(f"  [Video] 网络错误(第{attempt}次): {e}, 重试...")
                    time.sleep(NET_RETRY_DELAY)
                else:
                    raise
    finally:
        for fh in open_handles:
            try: fh.close()
            except Exception: pass

    logger.info(f"  [Video] 提交响应 HTTP {resp.status_code}")
    if resp.status_code == 200:
        try:
            body = resp.json()
            history_id = (body.get("uuid") or body.get("history_id")
                          or body.get("id") or body.get("task_id"))
            if not history_id:
                uuids = re.findall(
                    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                    resp.text,
                )
                history_id = uuids[0] if uuids else None
            logger.info(f"  [Video] history_id={history_id}")
            return (200, history_id, "")
        except Exception as e:
            logger.error(f"  [Video] 解析响应失败: {e}  body={resp.text[:200]}")
            return (200, None, "parse_error")
    elif resp.status_code == 429:
        _trigger_rate_limit()
        return (429, None, "RATE_LIMIT")
    elif resp.status_code in (401, 403):
        return (resp.status_code, None, "TOKEN_EXPIRED")
    elif resp.status_code == 400:
        try:
            detail = resp.json().get("detail", {})
            ec = (detail.get("error_code") if isinstance(detail, dict) else "") or ""
            if ec == "TURNSTILE_INVALID":
                return (400, None, "TURNSTILE_INVALID")
        except Exception:
            pass
        logger.error(f"  [Video] HTTP 400: {resp.text[:200]}")
        return (400, None, "")
    else:
        logger.error(f"  [Video] HTTP {resp.status_code}: {resp.text[:200]}")
        return (resp.status_code, None, "")


def _api_poll_video(history_id, save_path):
    """
    轮询 GET /api/history/{id} 直到完成，下载 .mp4 到 save_path。
    返回 (success, video_url, error_str)
    """
    start = time.time()
    consecutive_errors = 0

    while time.time() - start < VIDEO_GENERATE_TIMEOUT:
        elapsed = int(time.time() - start)
        token, _ = _get_fresh_token()
        if not token:
            logger.warning(f"  [Video] 无法获取 token，等待15s...")
            time.sleep(15)
            consecutive_errors += 1
            if consecutive_errors >= 5:
                return (False, None, "no_token")
            continue

        consecutive_errors = 0
        try:
            guard_id = _get_token_manager().get_guard_id(f"/api/history/{history_id}", "get")
            headers  = _build_headers(token, guard_id)
            resp = _safe_get(
                f"{API_BASE}/history/{history_id}",
                headers=headers, timeout=30
            )

            if resp.status_code == 200:
                data   = resp.json()
                status = data.get("status")
                logger.info(f"  [Video] [{elapsed}s] status={status}")

                if status == "success" or status == 2:
                    video_url = None
                    videos = data.get("generated_video") or []
                    if videos and isinstance(videos, list):
                        v = videos[0]
                        video_url = (v.get("file_download_url") or v.get("video_url"))
                    if not video_url:
                        video_url = (data.get("result_video_url") or data.get("video_url")
                                     or data.get("output_url") or data.get("url"))
                    if video_url:
                        logger.info(f"  [Video] 生成完成，下载 {video_url[:80]}...")
                        ok = _download_file(video_url, save_path)
                        return (ok, video_url, "")
                    else:
                        logger.error(f"  [Video] 状态成功但无 video_url: {data}")
                        return (False, None, "no_video_url")

                elif status == "failed" or status == 3:
                    err = data.get("error_msg") or data.get("error_message") or "未知错误"
                    logger.error(f"  [Video] 任务失败: {err}")
                    return (False, None, str(err)[:200])

            elif resp.status_code in (401, 403):
                logger.warning(f"  [Video] [{elapsed}s] HTTP {resp.status_code}，刷新 token...")
                _get_fresh_token(force_refresh=True, stale_token=token)
                time.sleep(15)
            elif resp.status_code in (500, 502, 503):
                logger.warning(f"  [Video] [{elapsed}s] 服务器错误 {resp.status_code}")
            else:
                logger.warning(f"  [Video] [{elapsed}s] HTTP {resp.status_code}")

        except Exception as e:
            consecutive_errors += 1
            logger.warning(f"  [Video] [{elapsed}s] 轮询异常({consecutive_errors}次): {e}")
            if consecutive_errors >= 3:
                _invalidate_token_cache(bad_token=token)
                time.sleep(30)
                consecutive_errors = 0

        time.sleep(VIDEO_POLL_INTERVAL)

    logger.error(f"  [Video] 超时 ({VIDEO_GENERATE_TIMEOUT}s)")
    return (False, None, "timeout")


def _download_file(url, save_path):
    """下载任意文件（图片或视频）到本地路径"""
    logger.info(f"  下载: {url[:80]}...")
    try:
        headers = {
            "Referer":    "https://geminigen.ai/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/147 Safari/537.36",
        }
        resp = _safe_get(url, headers=headers, timeout=120)
        resp.raise_for_status()
        with open(save_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
        size_mb = os.path.getsize(save_path) / 1024 / 1024
        logger.info(f"  已保存: {save_path} ({size_mb:.1f}MB)")
        return True
    except Exception as e:
        logger.error(f"  下载失败: {e}")
        return False


def run_video_task(save_path, prompt_text, model="veo-3-fast",
                   aspect_ratio="16:9", resolution="1080p",
                   duration=8, enhance_prompt=True,
                   mode_image="ingredient", ref_image_path=None):
    """
    提交视频生成任务并等待结果。
    save_path:      本地保存 .mp4 的路径
    prompt_text:    文字描述
    model:          grok-video | veo-3-fast
    aspect_ratio:   16:9 / 9:16 / 1:1 / landscape / portrait / square
    resolution:     480p / 720p / 1080p
    duration:       视频时长（秒）
    enhance_prompt: 是否开启 prompt 增强（Veo）
    mode_image:     ingredient / reference（Veo 有图时）
    ref_image_path: 本地参考图路径（可选，Veo 支持）
    返回: (success: bool, video_url: str|None, error_type: str)
    """
    MAX_SUBMIT_RETRY = 6

    try:
        history_id = None
        for attempt in range(1, MAX_SUBMIT_RETRY + 1):
            jitter = random.uniform(SUBMIT_JITTER_MIN, SUBMIT_JITTER_MAX)
            logger.info(f"  [Video] 提交前等待 {jitter:.1f}s...")
            time.sleep(jitter)

            _wait_for_rate_limit()

            token, _ = _get_fresh_token()
            if not token:
                logger.error("  [Video] Token 获取失败，等10s重试...")
                time.sleep(10)
                continue

            try:
                ts_token = _get_turnstile_token()
            except Exception as e:
                logger.error(f"  [Video] Turnstile 失败: {e}")
                return (False, None, "turnstile_capsolver_failed")

            _path    = "/api/video-gen/grok-stream" if model == "grok-video" else "/api/video-gen/veo"
            guard_id = _get_token_manager().get_guard_id(_path, "post")

            if guard_id:
                logger.info(f"  [Video] 提交（Python HTTP）  model={model}  attempt={attempt}/{MAX_SUBMIT_RETRY}")
                status_code, history_id, submit_err = _api_submit_video(
                    token, guard_id, ts_token, prompt_text,
                    model, aspect_ratio, resolution, duration,
                    enhance_prompt, mode_image, ref_image_path,
                )
            else:
                logger.info(f"  [Video] GuardIdGenerator 未就绪，降级浏览器 fetch  model={model}  attempt={attempt}/{MAX_SUBMIT_RETRY}")
                status_code, history_id, submit_err = _get_token_manager().submit_video_browser(
                    model, prompt_text, aspect_ratio, resolution, duration,
                    enhance_prompt, mode_image, ref_image_path,
                )

            if history_id:
                _turnstile_on_success()
                break

            if submit_err == "TURNSTILE_INVALID":
                _turnstile_on_skip_fail()
                logger.info("  [Video] skip 被拒，用 CapSolver 重试...")
                try:
                    ts_token = _solve_turnstile()
                except Exception as e:
                    logger.error(f"  [Video] CapSolver 失败: {e}")
                    return (False, None, "turnstile_capsolver_failed")
                guard_id = _get_token_manager().get_guard_id(_path, "post")
                if guard_id:
                    status_code, history_id, submit_err = _api_submit_video(
                        token, guard_id, ts_token, prompt_text,
                        model, aspect_ratio, resolution, duration,
                        enhance_prompt, mode_image, ref_image_path,
                    )
                else:
                    status_code, history_id, submit_err = _get_token_manager().submit_video_browser(
                        model, prompt_text, aspect_ratio, resolution, duration,
                        enhance_prompt, mode_image, ref_image_path,
                    )
                if history_id:
                    _turnstile_on_success()
                    break

            if submit_err == "RATE_LIMIT":
                _wait_for_rate_limit()
                continue
            if status_code in (401, 403):
                _get_fresh_token(force_refresh=True, stale_token=token)
                time.sleep(15)
                continue

            logger.warning(f"  [Video] 提交失败 HTTP {status_code} err={submit_err}，等10s重试...")
            time.sleep(10)

        if not history_id:
            logger.error(f"  [Video] 提交重试 {MAX_SUBMIT_RETRY} 次后仍失败")
            return (False, None, "submit_failed")

        logger.info(f"  [Video] 已提交，history_id={history_id}，开始轮询...")
        return _api_poll_video(history_id, save_path)

    except Exception as e:
        logger.error(f"  [Video] 任务异常: {e}")
        traceback.print_exc()
        if _is_network_error(e):
            _invalidate_token_cache()
        return (False, None, str(e)[:200])


# ============================================================
# 对外主接口
# ============================================================
def run_task(save_path, prompt_text, model=MODEL_PRIMARY,
             aspect_ratio="1:1", resolution="1K", reference_images=None):
    """
    提交一个图片生成任务并等待结果。
    save_path:        生成结果图片的本地保存路径
    prompt_text:      用户的文字描述
    model:            nano-banana-2 或 nano-banana-pro
    aspect_ratio:     1:1 / 16:9 / 9:16 / 3:4 / 4:3
    resolution:       1K / 2K / 4K
    reference_images: 本地图片路径列表（最多5张），作为参考图上传
    返回: (success: bool, thumbnail_url: str|None, error_type: str)
    """
    MAX_SUBMIT_RETRY = 10
    QUEUE_FULL_WAIT  = 30
    try:
        task_uuid = None
        for submit_attempt in range(1, MAX_SUBMIT_RETRY + 1):

            _check_model_auto_revert()
            current_model = model or _get_current_model()

            jitter = random.uniform(SUBMIT_JITTER_MIN, SUBMIT_JITTER_MAX)
            logger.info(f"  提交前错峰等待 {jitter:.1f}s...")
            time.sleep(jitter)

            _wait_for_rate_limit()

            token, _ = _get_fresh_token()
            if not token:
                logger.error("  Token 获取失败，等待10秒重试...")
                time.sleep(10); continue

            n_imgs = len(reference_images) if reference_images else 0
            logger.info(f"提交任务  ratio={aspect_ratio}  res={resolution}  model={current_model}  imgs={n_imgs}  attempt={submit_attempt}/{MAX_SUBMIT_RETRY}")
            try:
                status_code, task_uuid, submit_error = _get_token_manager().submit_generate(
                    prompt_text, aspect_ratio, resolution, model=current_model,
                    reference_images=reference_images
                )
            except Exception as e:
                if _is_network_error(e):
                    logger.warning(f"  网络错误，等10秒重试（第{submit_attempt}次）...")
                    _invalidate_token_cache(); time.sleep(10); continue
                raise

            if submit_error == "IMAGE_FORMAT_ERROR":
                return (False, None, "IMAGE_FORMAT_ERROR")
            if task_uuid:
                break

            if submit_error == "RATE_LIMIT":
                logger.warning("  限速，等待冷却后重试...")
                _wait_for_rate_limit(); continue

            if submit_error == "MAX_PROCESSING_IMAGEN_EXCEEDED":
                _on_model_rate_limited()
                current_model = _get_current_model()
                logger.warning(f"  并发超限，切换为 {current_model}，等{QUEUE_FULL_WAIT}s...")
                time.sleep(QUEUE_FULL_WAIT); continue

            if status_code in (401, 403):
                logger.info("  Token 过期，通知刷新后重试...")
                _get_fresh_token(force_refresh=True, stale_token=token)
                time.sleep(15)
                token, _ = _get_fresh_token()
                if not token: time.sleep(10); continue
                _check_model_auto_revert()
                current_model = model or _get_current_model()
                try:
                    status_code, task_uuid, submit_error = _get_token_manager().submit_generate(
                        prompt_text, aspect_ratio, resolution, model=current_model,
                        reference_images=reference_images
                    )
                except Exception as e:
                    if _is_network_error(e):
                        _invalidate_token_cache(); time.sleep(10); continue
                    raise
                if submit_error == "IMAGE_FORMAT_ERROR": return (False, None, "IMAGE_FORMAT_ERROR")
                if task_uuid: break
                if submit_error == "RATE_LIMIT":
                    _wait_for_rate_limit(); continue
                if submit_error == "MAX_PROCESSING_IMAGEN_EXCEEDED":
                    _on_model_rate_limited()
                    current_model = _get_current_model()
                    logger.warning(f"  并发超限，切换为 {current_model}，等{QUEUE_FULL_WAIT}s...")
                    time.sleep(QUEUE_FULL_WAIT); continue

            logger.warning(f"  提交失败(HTTP {status_code})，等10秒重试...")
            time.sleep(10)

        if not task_uuid:
            logger.error(f"  提交重试{MAX_SUBMIT_RETRY}次后仍失败")
            return (False, None, "")

        logger.info("等待结果...")
        return _api_poll_and_download(task_uuid, save_path)

    except Exception as e:
        logger.error(f"任务异常: {e}")
        traceback.print_exc()
        if _is_network_error(e): _invalidate_token_cache()
        return (False, None, "")
