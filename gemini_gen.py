# -*- coding: utf-8 -*-
"""
GeminiGen.ai 自动化模块 v3（纯Python HTTP + GuardId 自计算）
================================================================
核心改进：
1. 通过 init_script 劫持 crypto.subtle.digest，捕获 hY（随机 secretKey）
2. 通过 init_script 拦截 window.fetch，从首次 API 请求中提取 domFp
3. GuardIdGenerator：一次捕获，Python 永久生成 x-guard-id，不再用浏览器 fetch
4. 修复原版 _do_browser_generate 重复代码 bug
5. 浏览器仅用于：登录、Token 管理、一次性捕获 hY/domFp
"""

import base64
import hashlib
import json
import mimetypes
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

# Turnstile 自适应：连续 skip 失败几次后切换到 CapSolver
TURNSTILE_SKIP_FAIL_THRESHOLD = 2   # 连续失败多少次触发切换
SUBMIT_JITTER_MIN   = 1.0
SUBMIT_JITTER_MAX   = 4.0

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
# Model 自适应状态机（进程级，每账号独立）
# ============================================================
MODEL_PRIMARY          = "nano-banana-2"
MODEL_FALLBACK         = "nano-banana-pro"
MODEL_REVERT_INTERVAL  = 1800   # 30分钟后尝试回切

_current_model:      str   = MODEL_PRIMARY
_model_switched_at:  float = 0.0   # 切换到 fallback 的时间戳，0 表示未切换
_model_lock = threading.Lock()

def _get_current_model() -> str:
    """获取当前应使用的 model（线程安全）"""
    with _model_lock:
        return _current_model

def _on_model_rate_limited():
    """
    收到 MAX_PROCESSING_IMAGEN_EXCEEDED 时调用。
    - 若当前是 primary → 切换到 fallback，记录时间
    - 若已是 fallback  → 重置计时器（延后回切时间）
    """
    global _current_model, _model_switched_at
    with _model_lock:
        now = time.time()
        revert_at = time.strftime('%H:%M:%S', time.localtime(now + MODEL_REVERT_INTERVAL))
        if _current_model == MODEL_PRIMARY:
            _current_model      = MODEL_FALLBACK
            _model_switched_at  = now
            logger.warning(
                f"  🔄 [Model] {MODEL_PRIMARY} 触发限流，切换为 {MODEL_FALLBACK}，"
                f"将于 {revert_at} 尝试回切"
            )
        else:
            # 已是 fallback，重置计时器
            _model_switched_at = now
            logger.info(
                f"  🔄 [Model] 仍在限流，重置回切计时器，将于 {revert_at} 再次尝试回切"
            )

def _check_model_auto_revert():
    """
    每次提交前调用：若已切换到 fallback 且超过 30 分钟，尝试回切到 primary。
    回切后若再次限流，_on_model_rate_limited 会再次切换。
    """
    global _current_model, _model_switched_at
    with _model_lock:
        if (_current_model == MODEL_FALLBACK
                and _model_switched_at > 0
                and time.time() - _model_switched_at >= MODEL_REVERT_INTERVAL):
            _current_model     = MODEL_PRIMARY
            _model_switched_at = 0.0
            logger.info(f"  🔄 [Model] 30分钟已过，回切尝试 {MODEL_PRIMARY}...")

# ============================================================
# Turnstile 自适应状态（进程级）
# ============================================================
TURNSTILE_RESET_INTERVAL = 1800  # 30min 后重新尝试 skip

_turnstile_skip_fail_count: int   = 0      # 连续 skip 失败次数
_turnstile_use_capsolver:   bool  = False  # 是否已切换到 CapSolver
_turnstile_reset_at:        float = 0.0   # 到时自动重置为 skip 模式
_turnstile_lock = threading.Lock()

def _turnstile_on_skip_fail():
    """skip 被服务端拒绝时调用：计数+1，超阈值则切换 CapSolver 并设置重置定时器"""
    global _turnstile_skip_fail_count, _turnstile_use_capsolver, _turnstile_reset_at
    with _turnstile_lock:
        _turnstile_skip_fail_count += 1
        if (not _turnstile_use_capsolver and
                _turnstile_skip_fail_count >= TURNSTILE_SKIP_FAIL_THRESHOLD):
            _turnstile_use_capsolver = True
            _turnstile_reset_at      = time.time() + TURNSTILE_RESET_INTERVAL
            logger.warning(
                f"  🔄 [Turnstile] skip 连续失败 {_turnstile_skip_fail_count} 次，"
                f"切换 CapSolver 模式，将于 "
                f"{time.strftime('%H:%M:%S', time.localtime(_turnstile_reset_at))} 重试 skip"
            )

def _turnstile_on_success():
    """提交成功时调用：重置失败计数（不回退已切换的模式）"""
    global _turnstile_skip_fail_count
    with _turnstile_lock:
        _turnstile_skip_fail_count = 0

def _get_turnstile_token() -> str:
    """
    自适应获取 turnstile_token：
      - skip 模式：直接返回 'skip'，零开销
      - CapSolver 模式：调用 CapSolver 解码，返回真实 token
      - 定时重置：每 30min 自动回落到 skip 模式重新探测
    抛出异常由调用方处理
    """
    global _turnstile_use_capsolver, _turnstile_skip_fail_count, _turnstile_reset_at
    with _turnstile_lock:
        # 定时重置：到期后重新乐观尝试 skip
        if _turnstile_use_capsolver and time.time() >= _turnstile_reset_at:
            _turnstile_use_capsolver   = False
            _turnstile_skip_fail_count = 0
            logger.info("  🔄 [Turnstile] 定时重置，重新尝试 skip 模式")
        need_capsolver = _turnstile_use_capsolver

    if not need_capsolver:
        logger.debug("  [Turnstile] 使用 skip")
        return "skip"
    logger.info("  [Turnstile] 使用 CapSolver 解码...")
    return _solve_turnstile()  # 抛出异常由调用方处理

def _trigger_rate_limit():
    global _rate_limited_until
    with _rate_limit_lock:
        new_until = time.time() + RATE_LIMIT_COOLDOWN
        if new_until > _rate_limited_until:
            _rate_limited_until = new_until
            logger.warning(
                f"  ⏳ [限速冷却] 实例暂停提交 {RATE_LIMIT_COOLDOWN}s，"
                f"截止 {time.strftime('%H:%M:%S', time.localtime(new_until))}"
            )

def _wait_for_rate_limit():
    while True:
        with _rate_limit_lock:
            remaining = _rate_limited_until - time.time()
        if remaining <= 0:
            return
        logger.warning(f"  ⏳ [限速冷却] 还需等待 {remaining:.0f}s...")
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
      [0]      = 0x01 (pY 版本常量)
      [1:17]   = SHA256(hy:stable_id)[:32 hex] → 16字节（u，固定per session）
      [17:21]  = timeBucket uint32 big-endian（每60s变化一次）
      [21:53]  = SHA256(path:METHOD:u:timeBucket:hy) → 32字节（c，per请求）
      [53:85]  = domFp → 32字节（CSS指纹，固定per浏览器）

    hy 通过 init_script 劫持 crypto.subtle.digest 捕获。
    domFp 通过 init_script 拦截 fetch 从首次 API 请求中解码提取。
    """

    def __init__(self, hy: str, stable_id: str, dom_fp: str):
        self.hy        = hy
        self.stable_id = stable_id
        self.dom_fp    = dom_fp
        # u 固定 per session
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
        buf.append(1)                             # pY
        buf.extend(bytes.fromhex(self._u))        # 16B
        buf.extend(struct.pack(">I", tb))         # 4B
        buf.extend(bytes.fromhex(c))              # 32B
        buf.extend(bytes.fromhex(self.dom_fp))    # 32B

        return base64.urlsafe_b64encode(bytes(buf)).rstrip(b"=").decode()


# ============================================================
# 任务注册表
# ============================================================
_registry_lock  = threading.Lock()
_task_registry  = {}

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

# Turnstile 拦截（登录用）
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

# Guard-id 捕获脚本（每次创建 context 时注入）
_GUARD_CAPTURE_SCRIPT = """
(function() {
    window.__guard_hy_raw = null;   // "hY:stableId"
    window.__guard_dom_fp = null;   // 64 hex chars（32字节）

    // ── 工具：从 base64url guard-id 提取 domFp（bytes 53-85）──
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

    // ── 1. 劫持 crypto.subtle.digest 捕获 hY ──────────────────
    // Axios 拦截器调用 ab() → G3(hY:stableId) → crypto.subtle.digest
    if (window.crypto && window.crypto.subtle) {
        var _origDigest = window.crypto.subtle.digest.bind(window.crypto.subtle);
        window.crypto.subtle.digest = async function(algo, data) {
            var result = await _origDigest(algo, data);
            if (window.__guard_hy_raw === null) {
                try {
                    // fatal:false 不抛出，对非UTF8数据返回替换字符
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

    // ── 2. 劫持 XMLHttpRequest（Axios底层用XHR，不用fetch）──────
    var _origSetReqHdr = XMLHttpRequest.prototype.setRequestHeader;
    XMLHttpRequest.prototype.setRequestHeader = function(name, value) {
        if (window.__guard_dom_fp === null) {
            if (typeof name === 'string' && name.toLowerCase() === 'x-guard-id') {
                _extractDomFp(value);
            }
        }
        return _origSetReqHdr.call(this, name, value);
    };

    // ── 3. 同时兜底拦截 fetch（应对 Axios fetch adapter 或其他客户端）
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
        logger.error(f"  ❌ 打开登录页失败: {e}"); return False

    rsleep(2.0, 3.0); _close_popup(page)

    email_filled = False
    for sel in ['input[name="username"]','input[name="email"]',
                'input[type="email"]','input[placeholder*="email" i]']:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=2000):
                el.fill(USERNAME); rsleep(0.5, 1.0)
                email_filled = True; break
        except: continue
    if not email_filled:
        logger.error("  ❌ 找不到邮箱输入框"); return False

    pwd_filled = False
    for sel in ['input[name="password"]','input[type="password"]']:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=2000):
                el.fill(PASSWORD); rsleep(0.8, 1.5)
                pwd_filled = True; break
        except: continue
    if not pwd_filled:
        logger.error("  ❌ 找不到密码输入框"); return False

    logger.info("  解 Turnstile（CapSolver）...")
    try:
        ts_token = _solve_turnstile()
        logger.info(f"  ✅ Turnstile token（{len(ts_token)}字符）")
    except Exception as e:
        logger.error(f"  ❌ Turnstile 失败: {e}"); return False

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
        logger.error("  ❌ 无法提交表单"); return False

    logger.info("  等待登录结果...")
    deadline = time.time() + 60
    while time.time() < deadline:
        rsleep(2.0, 3.0)
        try:
            cur_url = page.url or ""
            for sel in ['[class*="error"]','[role="alert"]']:
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
        self._stable_id  = None   # guard_stable_id from localStorage
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

        # Guard-id 生成器（Python侧，一次初始化永久使用）
        self._guard_gen: GuardIdGenerator = None

        # Guard 初始化调度（非阻塞：在 run() 主循环里周期性尝试，不阻塞队列）
        self._guard_trigger_at: float = 0.0   # 到时后触发 SPA 请求
        self._guard_triggered:  bool  = False  # 是否已触发过

        # 浏览器提交队列
        self._gen_queue       = []
        self._gen_queue_lock  = threading.Lock()
        self._gen_queue_ready = threading.Event()

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
        """Worker 调用：获取指定 path/method 的 x-guard-id（Python纯计算）"""
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
            # 等待最多 1 秒，随时被 _gen_queue_ready 唤醒
            self._gen_queue_ready.wait(timeout=1.0)
            self._gen_queue_ready.clear()
            if self._stop_event.is_set(): break

            # ── 优先：处理提交队列（一次处理完所有积压项）──────────
            while True:
                item = None
                with self._gen_queue_lock:
                    if self._gen_queue:
                        item = self._gen_queue.pop(0)
                if item is None: break
                self._execute_browser_gen(item)

            # ── Token 刷新请求 ──────────────────────────────────────
            if self._refresh_request.is_set():
                self._refresh_request.clear()
                self._do_refresh()

            # ── 非阻塞 Guard 初始化（每次循环检查一次，不阻塞队列）──
            if self._guard_gen is None:
                now = time.time()
                # 触发 SPA 发请求（仅一次，延迟5s等页面稳定）
                if not self._guard_triggered and now >= self._guard_trigger_at > 0:
                    self._guard_triggered = True
                    self._trigger_spa_request()
                # 尝试从浏览器读取已捕获的 hY/domFp
                self._try_init_guard_gen()

            # ── 心跳 ───────────────────────────────────────────────
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
                        logger.info("TokenManager: ⚠ 需要登录...")
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

                    logger.info(f"TokenManager: ✅ 获得新 Token: {token[:30]}...")
                    self._set_token(token, stable_id)

                    # ── 调度 GuardIdGenerator 初始化（非阻塞）──
                    # 在 run() 主循环里每次迭代轮询，5 秒后触发 SPA 发请求
                    if self._guard_gen is None:
                        self._guard_triggered  = False
                        self._guard_trigger_at = time.time() + 5
                        logger.info("TokenManager: Guard 初始化已调度（5s 后触发 SPA）...")

                    return

                except Exception as e:
                    logger.error(f"TokenManager: 刷新异常（第{attempt+1}次）: {e}")
                    traceback.print_exc(); time.sleep(5)

            logger.error("TokenManager: ❌ Token 刷新连续失败3次")
        finally:
            with self._state_lock:
                self._refreshing = False
            self._token_ready.set()

    def _try_init_guard_gen(self):
        """
        非阻塞：尝试读取浏览器已捕获的 hY/domFp，成功则初始化 GuardIdGenerator。
        由 run() 主循环每次迭代调用，不阻塞队列处理。
        """
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
                logger.info("TokenManager: ✅ GuardIdGenerator 就绪，后续提交走纯 Python 路径")
                return True
        except Exception as e:
            logger.debug(f"_try_init_guard_gen: {e}")
        return False

    def _trigger_spa_request(self):
        """
        一次性触发 SPA 发 Axios 请求，让 XHR 拦截器捕获 x-guard-id 头（domFp）
        以及 crypto.subtle.digest 拦截器捕获 hY。
        由 run() 主循环在 guard_trigger_at 到达后调用一次。
        """
        logger.info("TokenManager: 主动触发 SPA API 请求（Vue router + Pinia）...")
        try:
            self._page.evaluate("""
                (async () => {
                    // 方法1：Vue router 重新导航（触发组件重挂载 + 数据重取）
                    try {
                        const el  = document.getElementById('__nuxt');
                        const app = el && el.__vue_app__;
                        if (app && app.config.globalProperties.$router) {
                            const router = app.config.globalProperties.$router;
                            await router.push('/app/imagen?_t=' + Date.now());
                            console.log('[GUARD] Router navigated');
                        }
                    } catch(e) { console.log('[GUARD] Router nav:', e); }

                    // 方法2：dispatchEvent
                    try {
                        document.dispatchEvent(new Event('visibilitychange'));
                        window.dispatchEvent(new Event('focus'));
                        window.dispatchEvent(new Event('online'));
                    } catch(e) {}

                    // 方法3：Pinia store 调用数据获取方法
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

    # 保留旧名供 _rebuild_context 复位状态用
    def _init_guard_generator(self):
        """已废弃的阻塞版本入口，仅复位调度状态（不再阻塞）"""
        self._guard_gen        = None
        self._guard_triggered  = False
        self._guard_trigger_at = time.time() + 5

    def _verify_guard_gen(self):
        """生成一个测试 guard-id 并验证结构（85字节，version=1）"""
        try:
            gid = self._guard_gen.generate("/api/test", "get")
            raw = base64.urlsafe_b64decode(gid + "====")
            assert len(raw) == 85, f"长度错误: {len(raw)}"
            assert raw[0] == 1, f"version byte 错误: {raw[0]}"
            logger.info(f"TokenManager: ✅ GuardIdGenerator 验证通过  sample={gid[:20]}...")
        except Exception as e:
            logger.error(f"TokenManager: GuardIdGenerator 验证失败: {e}")
            self._guard_gen = None

    def _heartbeat(self):
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
            headers = _build_headers(token, guard_id)
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

    # ── 提交队列（核心：Python HTTP + Python guard-id）────────

    def submit_via_browser(self, prompt_text, ratio, output_format="png", resolution="1K",
                           model=None, reference_image=None, timeout=300):
        """
        Worker线程调用：排队等待 TokenManager 线程执行提交。
        若 GuardIdGenerator 已就绪则纯 Python HTTP；否则降级浏览器 fetch（内置 ab()）。
        返回 (status_code, task_uuid, error_type)
        reference_image: 可选本地文件路径（0 或 1 张参考图）
        """
        if model is None:
            model = MODEL_PRIMARY
        event      = threading.Event()
        result_box = [None]
        with self._gen_queue_lock:
            self._gen_queue.append((event, result_box, reference_image, prompt_text, ratio, output_format, resolution, model))
            qlen = len(self._gen_queue)
        self._gen_queue_ready.set()
        if qlen > 1:
            logger.debug(f"TokenManager: 提交队列积压 {qlen} 项")
        if event.wait(timeout=timeout):
            return result_box[0] or (0, None, "empty_result")
        logger.error(f"TokenManager.submit_via_browser: 超时 ({timeout}s)，队列可能过载")
        return (0, None, "browser_timeout")

    def _execute_browser_gen(self, item):
        event, result_box, reference_image, prompt_text, ratio, output_format, resolution, model = item
        try:
            result_box[0] = self._do_generate(reference_image, prompt_text, ratio, output_format, resolution, model)
        except Exception as e:
            logger.error(f"TokenManager._execute_browser_gen 异常: {e}")
            result_box[0] = (0, None, "browser_exception")
        finally:
            event.set()

    def _do_generate(self, reference_image, prompt_text, ratio, output_format="png", resolution="1K", model=MODEL_PRIMARY):
        """
        提交 generate_image 请求。
        优先：Python requests + Python 生成的 guard-id（速度快、可靠）
        降级：浏览器 fetch（guard-id 由浏览器 Axios 拦截器自动生成）
        reference_image: 可选本地文件路径（None 或 字符串路径）
        """
        with self._state_lock:
            token = self._token
        if not token:
            return (0, None, "no_token")

        try:
            ts_token = _get_turnstile_token()
        except Exception as e:
            logger.error(f"  ❌ CapSolver 获取失败，本次跳过提交: {e}")
            return (0, None, "turnstile_capsolver_failed")

        logger.info(f"  [Model] 当前使用: {model}")

        # ── 方案A：Python HTTP（GuardIdGenerator 已就绪）──
        if self._guard_gen is not None:
            guard_id = self._guard_gen.generate("/api/generate_image", "post")
            logger.info(f"  [Python] 提交 generate_image  ts={ts_token[:8]}...  guard_id={guard_id[:20]}...")
            status_code, task_uuid, err = _api_generate_http(
                token, guard_id, reference_image, ts_token, prompt_text, ratio, output_format, resolution, model
            )
            if err == "TURNSTILE_INVALID":
                _turnstile_on_skip_fail()
                logger.info("  [Turnstile] skip 被拒，立即用 CapSolver 重试...")
                try:
                    ts_token = _solve_turnstile()
                except Exception as e:
                    logger.error(f"  ❌ CapSolver 重试获取失败: {e}")
                    return (status_code, None, "turnstile_capsolver_failed")
                guard_id = self._guard_gen.generate("/api/generate_image", "post")
                status_code, task_uuid, err = _api_generate_http(
                    token, guard_id, reference_image, ts_token, prompt_text, ratio, output_format, resolution, model
                )
            if err == "" and task_uuid:
                _turnstile_on_success()
            return (status_code, task_uuid, err)

        # ── 方案B：浏览器 fetch 降级（guard-id 由 Axios 拦截器自动生成）──
        logger.info("  [Browser] GuardIdGenerator 未就绪，降级使用浏览器 fetch...")
        if not self._is_context_alive():
            self._rebuild_context()
            if not self._is_context_alive():
                return (0, None, "browser_dead")
        status_code, task_uuid, err = self._do_browser_fetch(
            token, reference_image, prompt_text, ratio, output_format, resolution, ts_token, model
        )
        if err == "TURNSTILE_INVALID":
            _turnstile_on_skip_fail()
            logger.info("  [Turnstile][Browser] skip 被拒，立即用 CapSolver 重试...")
            try:
                ts_token = _solve_turnstile()
            except Exception as e:
                logger.error(f"  ❌ CapSolver 重试获取失败: {e}")
                return (status_code, None, "turnstile_capsolver_failed")
            status_code, task_uuid, err = self._do_browser_fetch(
                token, reference_image, prompt_text, ratio, output_format, resolution, ts_token, model
            )
        if err == "" and task_uuid:
            _turnstile_on_success()
        return (status_code, task_uuid, err)

    def _do_browser_fetch(self, token, reference_image, prompt_text, ratio,
                          output_format="png", resolution="1K", ts_token="skip", model=MODEL_PRIMARY):
        """
        在浏览器页面内直接注入并调用 ab() 完整实现，生成正确的 x-guard-id，
        然后用带该 header 的 fetch 提交请求。
        reference_image: 可选本地路径（None 或 字符串）
        ts_token: Turnstile token，传 "skip" 或 CapSolver 真实 token
        """
        import base64 as _b64
        ref_data = ref_mime = ref_name = None
        if reference_image:
            try:
                with open(reference_image, 'rb') as f:
                    ref_data = _b64.b64encode(f.read()).decode()
                ref_mime = mimetypes.guess_type(reference_image)[0] or "image/png"
                ref_name = os.path.basename(reference_image)
            except Exception as e:
                logger.error(f"  [Browser] 读取参考图失败: {e}")
                return (0, None, "file_read_error")

        try:
            result = self._page.evaluate("""
                async function(args) {
                    const {token, prompt, ratio, outputFormat, resolution, tsToken, model,
                           refB64, refMime, refName} = args;

                    // ── ab() 完整实现（逆向自 D6qDEc1Pjs）─────────────────────

                    // SHA-256，返回 64 hex chars
                    async function G3(str) {
                        const data = new TextEncoder().encode(str);
                        const buf  = await crypto.subtle.digest('SHA-256', data);
                        return Array.from(new Uint8Array(buf))
                            .map(b => b.toString(16).padStart(2,'0')).join('');
                    }

                    // hex string → byte array
                    function bm(hex) {
                        const arr = [];
                        for (let i = 0; i < hex.length; i += 2)
                            arr.push(parseInt(hex.substr(i, 2), 16));
                        return arr;
                    }

                    // uint32 → 4 bytes big-endian
                    function lY(n) {
                        return [(n>>>24)&255, (n>>>16)&255, (n>>>8)&255, n&255];
                    }

                    // byte array → base64url（无 padding）
                    function IY(bytes) {
                        return btoa(String.fromCharCode(...bytes))
                            .replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=+$/,'');
                    }

                    // 读取 / 生成 guard_stable_id（与原 fY 逻辑一致）
                    async function fY() {
                        const KEY   = 'guard_stable_id';
                        const VALID = /^[A-Za-z0-9_-]{22}$/;
                        try {
                            const cached = localStorage.getItem(KEY);
                            if (cached && VALID.test(cached)) return cached;
                        } catch(e) {}
                        // 重新生成
                        const rand  = Array.from(crypto.getRandomValues(new Uint8Array(16)))
                                          .map(b=>b.toString(16).padStart(2,'0')).join('');
                        const ua    = navigator.userAgent || 'unknown';
                        const sc    = (screen.width||0) + 'x' + (screen.height||0);
                        const hash  = await G3('default:' + rand + ':' + ua + ':' + sc);
                        const newId = IY(bm(hash)).slice(0, 22);
                        try { localStorage.setItem(KEY, newId); } catch(e) {}
                        return newId;
                    }

                    // DOM 指纹（简化版 EY：用 screen + navigator + canvas）
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
                        return await G3(combined);   // 64 hex chars
                    }

                    // 核心 guard-id 生成（对应原 wY + ab）
                    async function ab(path, method) {
                        const stableId   = await fY();
                        const timeBucket = Math.floor(Date.now() / 60000);
                        const domFp      = await EY();

                        // secretKey 从 Nuxt public.antibot.secretKey 获取
                        // 抓包确认 config 为空 {}，所以退化为 "" → CY("") = hY（随机）
                        // 但服务器无法验证 hY，只需结构正确 + timeBucket 有效
                        let secretKey = '';
                        try {
                            // 尝试读取真实 secretKey（若 Nuxt config 非空则更准确）
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
                        buf[0] = 1;                              // pY 版本常量
                        bm(u).forEach((v,i)  => buf[i+1]  = v); // [1:17]
                        lY(timeBucket).forEach((v,i) => buf[i+17] = v); // [17:21]
                        bm(c).forEach((v,i)  => buf[i+21] = v); // [21:53]
                        bm(domFp).forEach((v,i) => buf[i+53] = v); // [53:85]
                        return IY(Array.from(buf));
                    }

                    // ── 构建 FormData ────────────────────────────────────────
                    function b64toBlob(b64, mime) {
                        const raw = atob(b64);
                        const buf = new Uint8Array(raw.length);
                        for (let i = 0; i < raw.length; i++) buf[i] = raw.charCodeAt(i);
                        return new Blob([buf], {type: mime});
                    }
                    const fd = new FormData();
                    fd.append('prompt',          prompt);
                    fd.append('model',           model);
                    fd.append('aspect_ratio',    ratio);
                    fd.append('output_format',   outputFormat);
                    fd.append('resolution',      resolution);
                    fd.append('turnstile_token', tsToken);
                    if (refB64 && refMime && refName) {
                        fd.append('files', b64toBlob(refB64, refMime), refName);
                    }

                    // ── 生成 guard-id 并提交 ─────────────────────────────────
                    try {
                        const guardId = await ab('/api/generate_image', 'post');
                        console.log('[ab] guard-id generated:', guardId.slice(0, 20) + '...');

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
                "token": token, "prompt": prompt_text, "ratio": ratio,
                "outputFormat": output_format, "resolution": resolution,
                "tsToken": ts_token, "model": model,
                "refB64": ref_data, "refMime": ref_mime, "refName": ref_name,
            })
        except Exception as e:
            logger.error(f"  [Browser] page.evaluate 异常: {e}")
            return (0, None, "evaluate_error")

        return self._parse_generate_response(result.get("status", 0), result.get("body", ""))

    @staticmethod
    def _parse_generate_response(status, body):
        if status == 200:
            try:
                data = json.loads(body)
                task_uuid = data.get("uuid") or data.get("id")
                if not task_uuid:
                    uuids = re.findall(
                        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                        body
                    )
                    task_uuid = uuids[0] if uuids else None
                if task_uuid:
                    logger.info(f"  ✅ UUID: {task_uuid}")
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
                    logger.warning("  ⚠ 服务端要求真实 Turnstile token")
                    return (400, None, "TURNSTILE_INVALID")
                if error_code == "MAX_PROCESSING_IMAGEN_EXCEEDED":
                    logger.error(f"  ❌ HTTP 400: {body[:200]}")
                    return (400, None, "MAX_PROCESSING_IMAGEN_EXCEEDED")
            except: pass
            logger.error(f"  ❌ HTTP 400: {body[:200]}")
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
            logger.error(f"  ❌ HTTP {status}: {body[:200]}")
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
                "--no-sandbox","--disable-blink-features=AutomationControlled",
                "--lang=zh-CN,zh,en-US,en","--window-size=1920,1080",
                "--disable-extensions","--disable-translate",
            ],
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN", ignore_https_errors=True,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
            ),
        )
        # 反检测
        ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins',   { get: () => [1,2,3,4,5] });
            window.chrome = { runtime: {} };
        """)
        # Guard-id 捕获（每次页面加载前注入）
        ctx.add_init_script(_GUARD_CAPTURE_SCRIPT)

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
            self._token = None; self._stable_id = None; self._token_time = 0.0
        self._guard_gen = None   # 重建浏览器后需重新捕获
        self._guard_triggered  = False
        self._guard_trigger_at = 0.0
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
    if guard_id: h["x-guard-id"] = guard_id
    return h


# ============================================================
# CapSolver
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
# Python HTTP 提交图片生成
# ============================================================
def _api_generate_http(token, guard_id, reference_image,
                       turnstile_token, prompt_text, ratio,
                       output_format="png", resolution="1K", model=MODEL_PRIMARY):
    """纯 Python requests 提交，使用 Python 生成的 guard-id。reference_image 可为 None。"""
    url     = f"{API_BASE}/generate_image"
    headers = _build_headers(token, guard_id)
    logger.info(f"  [Python] ratio={ratio}  fmt={output_format}  res={resolution}  model={model}  guard_id={guard_id[:20]}...")

    fields = [
        ("prompt",          (None, prompt_text)),
        ("model",           (None, model)),
        ("aspect_ratio",    (None, ratio)),
        ("output_format",   (None, output_format)),
        ("resolution",      (None, resolution)),
        ("turnstile_token", (None, turnstile_token)),
    ]
    file_handles = []
    if reference_image:
        fname = os.path.basename(reference_image)
        mime  = mimetypes.guess_type(reference_image)[0] or "image/png"
        fh    = open(reference_image, "rb")
        file_handles.append(fh)
        fields.append(("files", (fname, fh, mime)))
    try:
        resp = _safe_post_files(url, headers, fields, timeout=120)
    finally:
        for fh in file_handles: fh.close()

    logger.info(f"  API响应: HTTP {resp.status_code}")
    return _TokenManager._parse_generate_response(resp.status_code, resp.text)


# ============================================================
# 轮询 + 下载
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
                        logger.info("  ✅ 生成完成（被其他 worker 发现）!")
                        return (_download_image(result["thumbnail_url"], save_path),
                                result["thumbnail_url"], "")
                    elif result["status"] == 3:
                        ec = result.get("error_code", "") or ""
                        em = result.get("error_message", "") or ""
                        logger.error(f"  ❌ 任务失败: {ec} - {em}")
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
                    logger.error("  ❌ 连续5次无法获取 token，放弃")
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

        logger.error(f"  ❌ 超时 ({GENERATE_TIMEOUT}s)")
        return (False, None, "")
    finally:
        _unregister_task(task_uuid)


def _download_image(url, save_path):
    logger.info(f"  下载: {url[:80]}...")
    try:
        headers = {
            "Referer":    "https://geminigen.ai/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/147 Safari/537.36"
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
def run_task(save_path, prompt_text, reference_image=None,
             aspect_ratio="1:1", output_format="png", resolution="1K", model=None):
    """
    对外主接口。
    save_path:       生成图片本地保存路径
    prompt_text:     用户提示词
    reference_image: 可选参考图本地路径（None 表示不上传）
    aspect_ratio:    纵横比，如 "1:1", "16:9", "3:4" 等
    output_format:   输出格式，"png" 或 "jpeg"
    resolution:      分辨率，"1K" / "2K" / "4K"
    model:           使用的模型，None 则使用当前状态机模型
    """
    MAX_SUBMIT_RETRY = 10
    QUEUE_FULL_WAIT  = 30
    try:
        task_uuid = None
        for submit_attempt in range(1, MAX_SUBMIT_RETRY + 1):

            # 每次提交前检查是否可回切到 primary model
            _check_model_auto_revert()
            current_model = model if model else _get_current_model()

            jitter = random.uniform(SUBMIT_JITTER_MIN, SUBMIT_JITTER_MAX)
            logger.info(f"  提交前错峰等待 {jitter:.1f}s...")
            time.sleep(jitter)

            _wait_for_rate_limit()

            token, _ = _get_fresh_token()
            if not token:
                logger.error("  Token 获取失败，等待10秒重试...")
                time.sleep(10); continue

            logger.info(
                f"▷ 提交任务  ratio={aspect_ratio}  fmt={output_format}  res={resolution}"
                f"  model={current_model}  attempt={submit_attempt}/{MAX_SUBMIT_RETRY}"
            )
            try:
                status_code, task_uuid, submit_error = _get_token_manager().submit_via_browser(
                    prompt_text, aspect_ratio, output_format, resolution,
                    model=current_model, reference_image=reference_image
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
                logger.warning(f"  ⚠ 限速，等待冷却后重试...")
                _wait_for_rate_limit(); continue

            if submit_error == "MAX_PROCESSING_IMAGEN_EXCEEDED":
                _on_model_rate_limited()
                current_model = model if model else _get_current_model()
                logger.warning(f"  ⏳ 并发超限，切换为 {current_model}，等{QUEUE_FULL_WAIT}s...")
                time.sleep(QUEUE_FULL_WAIT); continue

            if status_code in (401, 403):
                logger.info("  Token 过期，通知刷新后重试...")
                _get_fresh_token(force_refresh=True, stale_token=token)
                time.sleep(15)
                token, _ = _get_fresh_token()
                if not token: time.sleep(10); continue
                _check_model_auto_revert()
                current_model = model if model else _get_current_model()
                try:
                    status_code, task_uuid, submit_error = _get_token_manager().submit_via_browser(
                        prompt_text, aspect_ratio, output_format, resolution,
                        model=current_model, reference_image=reference_image
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
                    current_model = model if model else _get_current_model()
                    logger.warning(f"  ⏳ 并发超限，切换为 {current_model}，等{QUEUE_FULL_WAIT}s...")
                    time.sleep(QUEUE_FULL_WAIT); continue

            logger.warning(f"  提交失败(HTTP {status_code})，等10秒重试...")
            time.sleep(10)

        if not task_uuid:
            logger.error(f"  提交重试{MAX_SUBMIT_RETRY}次后仍失败")
            return (False, None, "")

        logger.info("▷ 等待结果...")
        return _api_poll_and_download(task_uuid, save_path)

    except Exception as e:
        logger.error(f"任务异常: {e}")
        traceback.print_exc()
        if _is_network_error(e): _invalidate_token_cache()
        return (False, None, "")