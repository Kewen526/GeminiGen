# -*- coding: utf-8 -*-
"""
AI质量检测模块（2维度，3次机会，任一合格即通过）
================================================
quality_check(scene_photo_path, product_url, generated_url):
  图1=场景参考图(本地→上传), 图2=电商原图URL, 图3=生成图URL
  调用AI最多3次，只要有1次合格 → 最终合格
"""

import os
import re
import json
import asyncio
import aiohttp
import time
import logging
import threading
import hashlib
import requests
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# ============================================================
# 配置
# ============================================================
ZHIPU_MODEL   = "glm-4.1v-thinking-flash"
ZHIPU_KEY_API = "http://47.95.157.46:8520/api/zhipuai_key"
ZHIPU_API_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"

# 场景图上传配置（360图床优先，COS兜底）
COS_SECRET_ID  = "AKIDrYz93g26vUmpb6KHxMULvFI4aonVw60d"
COS_SECRET_KEY = "OVMwFH1astc4FApEMCm47tOcaGfnfFXZ"
COS_REGION     = "ap-beijing"
COS_BUCKET     = "ceshi-1300392622"
SCENE_CACHE_FILE = r"C:\scene_photo_cache.json"

VOTE_TOTAL = 3
VOTE_PASS  = 1   # 只要有1次合格即通过


# ============================================================
# 场景图上传（360优先，COS兜底）+ 本地缓存
# ============================================================
_scene_cache_lock = threading.Lock()
_scene_url_cache  = {}
_cache_loaded     = False


def _load_scene_cache():
    global _scene_url_cache, _cache_loaded
    if _cache_loaded:
        return
    try:
        if os.path.exists(SCENE_CACHE_FILE):
            with open(SCENE_CACHE_FILE, "r", encoding="utf-8") as f:
                _scene_url_cache = json.load(f)
            logger.info(f"  加载场景图缓存: {len(_scene_url_cache)} 条")
    except Exception as e:
        logger.warning(f"  加载场景图缓存失败: {e}")
        _scene_url_cache = {}
    _cache_loaded = True


def _save_scene_cache():
    try:
        cache_dir = os.path.dirname(SCENE_CACHE_FILE)
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)
        with open(SCENE_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_scene_url_cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"  保存场景图缓存失败: {e}")


def _upload_scene_to_360(local_path: str) -> Optional[str]:
    """单次360图床上传，返回URL或None。"""
    try:
        filename = local_path.split("\\")[-1].split("/")[-1]
        with open(local_path, 'rb') as f:
            resp = requests.post(
                "https://api.xinyew.cn/api/360tc",
                files={"file": (filename, f, "image/png")},
                proxies={"http": None, "https": None},
                timeout=15,
            )
        j = resp.json()
        if j.get("errno") == 0:
            url = j["data"]["url"]
            logger.info(f"  场景图360图床上传成功: {url[:60]}...")
            return url
        else:
            logger.warning(f"  场景图360图床失败: {j.get('error')}")
            return None
    except Exception as e:
        logger.warning(f"  场景图360图床异常: {e}")
        return None


def _upload_scene_to_cos(local_path: str) -> Optional[str]:
    """COS兜底上传，返回URL或None。"""
    try:
        from qcloud_cos import CosConfig, CosS3Client
        config = CosConfig(
            Region=COS_REGION, SecretId=COS_SECRET_ID, SecretKey=COS_SECRET_KEY,
            Token=None, Proxies={"http": None, "https": None}
        )
        client  = CosS3Client(config)
        basename  = os.path.basename(local_path)
        path_hash = hashlib.md5(local_path.encode()).hexdigest()[:8]
        cos_key   = f"scene_photos/{path_hash}_{basename}"
        client.put_object_from_local_file(Bucket=COS_BUCKET, LocalFilePath=local_path, Key=cos_key)
        url = f"https://{COS_BUCKET}.cos.{COS_REGION}.myqcloud.com/{cos_key}"
        logger.info(f"  场景图COS兜底上传成功: {url[:60]}...")
        return url
    except Exception as e:
        logger.error(f"  场景图COS兜底上传失败: {e}")
        return None


def upload_scene_to_cos(local_path: str) -> Optional[str]:
    """
    上传场景图：360图床（第1次）→ 360图床（第2次）→ COS兜底，带本地缓存。
    """
    with _scene_cache_lock:
        _load_scene_cache()
        if local_path in _scene_url_cache:
            return _scene_url_cache[local_path]

    # 第1次：360
    url = _upload_scene_to_360(local_path)
    # 第2次：360重试
    if not url:
        logger.warning("  场景图360第1次失败，重试第2次...")
        url = _upload_scene_to_360(local_path)
    # 兜底：COS
    if not url:
        logger.warning("  场景图360两次均失败，降级COS兜底...")
        url = _upload_scene_to_cos(local_path)

    if url:
        with _scene_cache_lock:
            _scene_url_cache[local_path] = url
            _save_scene_cache()

    return url


# ============================================================
# ZhipuAI密钥
# ============================================================
_cached_keys = []
_keys_lock   = threading.Lock()


def _get_zhipu_key() -> str:
    global _cached_keys
    with _keys_lock:
        if not _cached_keys:
            try:
                resp = requests.post(
                    ZHIPU_KEY_API,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    data="", timeout=10, proxies={"http": None, "https": None}
                )
                _cached_keys = [item["key"] for item in resp.json()["data"]]
                logger.info(f"  获取到 {len(_cached_keys)} 个ZhipuAI密钥")
            except Exception as e:
                logger.error(f"  获取ZhipuAI密钥失败: {e}")
                return ""
    import random
    return random.choice(_cached_keys) if _cached_keys else ""


# ============================================================
# ZhipuAI调用
# ============================================================
async def _call_zhipu_vision(image_urls: list, prompt: str) -> Optional[str]:
    api_key = _get_zhipu_key()
    if not api_key:
        logger.error("  无可用ZhipuAI密钥")
        return None

    content = []
    for url in image_urls:
        if url:
            content.append({"type": "image_url", "image_url": {"url": url}})
    content.append({"type": "text", "text": prompt})

    payload = {
        "model": ZHIPU_MODEL,
        "temperature": 0.0,
        "max_tokens": 2000,
        "messages": [{"role": "user", "content": content}]
    }

    for attempt in range(3):
        try:
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=connector, trust_env=False) as session:
                async with session.post(
                    ZHIPU_API_URL,
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json=payload, timeout=120
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data["choices"][0]["message"]["content"]
                    elif resp.status == 429:
                        logger.warning("  ZhipuAI限流(429)，换key重试...")
                        api_key = _get_zhipu_key()
                        await asyncio.sleep(3)
                    else:
                        text = await resp.text()
                        logger.error(f"  ZhipuAI HTTP {resp.status}: {text[:200]}")
                        await asyncio.sleep(2)
        except Exception as e:
            logger.warning(f"  ZhipuAI调用异常({attempt+1}): {e}")
            await asyncio.sleep(3)
    return None


# ============================================================
# 质量检测提示词（2维度，宽松认亲逻辑）
# ============================================================
QUALITY_CHECK_PROMPT = """## 前置说明
图3是将图2的商品提取后、替换放入图1场景中生成的AI合成图。
图2中可能包含多个商品，检测前先识别主体商品（最居中、最完整的那一件），只针对该主体商品进行检测。

检测逻辑：只要商品核心特征认得出（像同一款），且不是"悬浮"在背景上的，即为合格。

---

## 维度一：商品身份识别（认亲不找茬）

检测重点：视觉上是否认定为图2中的同一件商品。

宽容规则：允许微小细节（如缝线、拉链、细小文字）的模糊或轻微偏移。

通过条件：主色调一致、主要轮廓一致、核心装饰元素/Logo位置大致合理。

判定：[合格] / [不合格：货不对板/变样了]

---

## 维度二：物理存在合理性（落地不悬浮）

检测重点：商品是否真实"存在"于场景中，而非"贴上去"的。

通过条件：
1. 有接触点：商品与场景（地面/货架/纸箱）有明确的物理接触
2. 有阴影：接触面有自然的阴影（哪怕是很简单的暗影）
3. 无边缘异样：边缘没有抠图白边、锯齿或异常的模糊
4. 比例合理：商品大小与场景空间没有严重失调

判定：[合格] / [不合格：悬浮感明显/抠图痕迹重/比例严重失调]

---

## 输出格式

**商品身份识别：** [合格] / [不合格]
（如不合格，简述变样的具体位置）

**物理存在合理性：** [合格] / [不合格]
（如不合格，简述是悬浮、抠图痕迹还是比例问题）

**总体结论：** [合格] / [不合格]

**不合格原因：**
（如不合格，简述是商品变样了，还是悬浮不自然）"""


# ============================================================
# 解析AI回复
# ============================================================
def _clean_ai_result(text: str) -> str:
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    answer_match = re.search(r'<answer>(.*?)</answer>', text, re.DOTALL)
    if answer_match:
        return answer_match.group(1).strip()
    return text.strip()


def _parse_quality_result(ai_reply: str) -> Tuple[bool, str]:
    """
    解析AI质量检测结果，提取"总体结论"。
    新格式使用 [合格] / [不合格] 带方括号。
    """
    cleaned = _clean_ai_result(ai_reply)

    # 策略1：精确匹配"总体结论：[合格/不合格]"（带方括号新格式）
    patterns = [
        r'总体结论[：:]\s*\[?(不合格|合格)\]?',
        r'\*{0,2}总体结论\*{0,2}[：:\s]\s*\[?(不合格|合格)\]?',
        r'总体[结论判断断][：:]\s*\[?(不合格|合格)\]?',
    ]
    for pattern in patterns:
        m = re.search(pattern, cleaned, re.IGNORECASE)
        if m:
            result_text = m.group(1).strip().strip('[]').strip('*').strip()
            is_pass = result_text == "合格"
            reason  = _extract_fail_reason(cleaned) if not is_pass else ""
            return (is_pass, reason)

    # 策略2：根据两个维度判断推断总体
    # 只要任意一个维度出现 [不合格] 标记，即整体不合格
    if re.search(r'\[不合格[：:：]?', cleaned):
        reason = _extract_fail_reason(cleaned)
        logger.info(f"  质量检测解析（维度推断）: 不合格")
        return (False, reason or "维度不合格")

    # 策略3：兜底，找最后出现的"合格"或"不合格"
    all_fail = list(re.finditer(r'不合格', cleaned))
    all_pass = list(re.finditer(r'(?<!不)合格', cleaned))
    if all_fail or all_pass:
        last_fail = all_fail[-1].start() if all_fail else -1
        last_pass = all_pass[-1].start() if all_pass else -1
        if last_fail > last_pass:
            logger.info("  质量检测解析（兜底）: 不合格")
            return (False, "AI判定不合格")
        else:
            logger.info("  质量检测解析（兜底）: 合格")
            return (True, "")

    logger.warning(f"  质量检测解析失败，AI回复前300字: {cleaned[:300]}")
    return (False, "解析失败")


def _extract_fail_reason(cleaned: str) -> str:
    """从AI回复中提取不合格原因"""
    for rp in [r'不合格原因[：:]\s*(.*?)(?:\n\n|\Z)', r'不合格[：:]\s*(.*?)(?:\n\n|\Z)']:
        rm = re.search(rp, cleaned, re.DOTALL)
        if rm:
            return rm.group(1).strip()[:200]
    return "不合格"


# ============================================================
# 对外接口：质量检测（2维度，最多3次，任一合格即通过）
# ============================================================
def quality_check(scene_photo_path: str, product_url: str, generated_url: str) -> Tuple[bool, str]:
    """
    对生成图进行AI质量检测。
    scene_photo_path: 本地场景图路径（上传获取URL）
    product_url:      电商原图URL
    generated_url:    生成图URL
    返回: (合格bool, 原因摘要str)
    """
    logger.info("  🔎 AI质量检测（2维度，最多3次，任一合格即通过）...")

    # 上传场景图
    scene_url = upload_scene_to_cos(scene_photo_path)
    if not scene_url:
        logger.warning("  场景图上传失败，跳过质量检测，视为合格")
        return (True, "")

    # 3张图：图1=场景图, 图2=电商图, 图3=生成图
    image_urls = [scene_url, product_url, generated_url]

    last_reason  = ""
    vote_results = []

    for vote_round in range(1, VOTE_TOTAL + 1):
        logger.info(f"  质检第{vote_round}/{VOTE_TOTAL}次...")

        ai_reply = asyncio.run(_call_zhipu_vision(image_urls, QUALITY_CHECK_PROMPT))

        if not ai_reply:
            logger.warning(f"  第{vote_round}次：AI无回复，视为不合格")
            last_reason = "AI无回复"
            vote_results.append("不合格(无回复)")
        else:
            passed, reason = _parse_quality_result(ai_reply)
            if passed:
                vote_results.append("合格")
                logger.info(f"  第{vote_round}次：✅ 合格 → 提前通过")
                logger.info(f"  质检结果: {vote_results} → ✅ 合格")
                return (True, "")
            else:
                last_reason = reason
                vote_results.append(f"不合格({reason[:30]})")
                logger.info(f"  第{vote_round}次：❌ 不合格 - {reason[:50]}")

    logger.info(f"  质检结果: {vote_results} → ❌ 不合格")
    return (False, last_reason)