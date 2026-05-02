# -*- coding: utf-8 -*-
"""
ZhipuAI 商品图片分类模块（17分类，含1688标题辅助）
"""

import re
import asyncio
import aiohttp
import time
import logging
import threading
import requests
from urllib import parse
from functools import wraps
from typing import Optional, List

logger = logging.getLogger(__name__)

# ============================================================
# 分类提示词模板（{title_1688} 会被替换为实际标题）
# ============================================================
CLASSIFICATION_PROMPT_TEMPLATE = """你是一个跨境电商商品图片分类专家。你将收到以下输入：
商品图片（主要依据）
1688商品标题（重要辅助，通常包含准确的品类、材质、用途关键词）
请根据图片和1688标题，从以下 17个固定分类 中选择最匹配的一个。
1688商品标题：{title_1688}

判断流程（必须按顺序执行）
第一步：提取1688标题关键词 从1688标题中提取：品类词（如"连衣裙""耳机""手机壳"）、材质词（如"硅胶""真皮""不锈钢"）、用途词（如"收纳""装饰""保暖"）。
第二步：图片主体识别 识别图片中最居中、最突出的商品主体，判断其外观形态。
第三步：交叉验证
1688标题关键词 与 图片主体 一致 → 直接分类
1688标题关键词 与 图片主体 矛盾 → 以图片实物为准，但需在分析中说明矛盾点
图片模糊/无法辨认 → 以1688标题为准
图片中有多个商品 → 以最居中、最突出的为主体
第四步：套用分类规则 按下方分类列表逐一匹配，注意每个分类的特殊规则。

分类列表（17类）
1. 包
手提包、双肩包、斜挎包、钱包、腰包、公文包、旅行包、化妆包、收纳包、卡包、零钱包、收纳袋等一切箱包类。
化妆包 → 包（不归"化妆品"）；收纳袋/收纳包 → 包（不归"家居"）
2. 保健穿戴
按摩仪、护腰、护膝、护腕、矫正带、磁疗产品、筋膜枪、颈椎枕、电动牙刷、理疗仪、体温计等保健类；智能手环/手表（侧重运动健康监测功能）。
传统装饰手表 → 戒指_项链_耳钉_手链；运动护具（护膝、护腕等） → 保健穿戴
3. 电子
手机、耳机、平板、电脑配件、充电器、数据线、充电宝、音箱、鼠标、键盘、USB设备、LED灯条、电子秤、平板保护壳等。
手机壳 → 手机壳（不归这里）；平板保护壳 → 电子
4. 发饰饰品
发夹、发圈、发箍、头绳、发带（窄条箍头型）、头花、抓夹、BB夹、U型夹、鲨鱼夹、胸针、别针、徽章、领带夹等。
项链/手链/耳钉 → 戒指_项链_耳钉_手链；宽版布艺头巾 → 服饰_上衣
5. 服饰_裤子
牛仔裤、休闲裤、运动裤、短裤、裙裤、打底裤、阔腿裤、西装裤、半身裙等下半身穿着。
连体裤 → 服饰_连衣裙；半身裙 → 服饰_裤子
6. 服饰_连衣裙
连衣裙、旗袍、长裙（连身型）、连体裤、连体衣、裙装套装（上下配套出售）、吊带裙。
半身裙（非连身） → 服饰_裤子；上下装套装（配套出售） → 服饰_连衣裙
7. 服饰_上衣
T恤、衬衫、卫衣、外套、羽绒服、夹克、毛衣、马甲、背心（外穿型）、风衣、围巾、丝巾、宽版头巾等上半身服装。
围巾/丝巾 → 服饰_上衣；宽版布艺头巾 → 服饰_上衣
8. 挂件
钥匙扣、手机挂饰、包包挂件、车载挂件、装饰吊坠（非首饰类）、毛绒挂件、风铃、捕梦网等。
项链吊坠（首饰） → 戒指_项链_耳钉_手链
9. 化妆品
护肤品、彩妆、香水、美甲产品、化妆刷、美妆蛋、假睫毛、面膜、洗面奶等。
化妆包 → 包
10. 家居
收纳盒/架、厨房用品、浴室用品、装饰摆件、花瓶、床上用品、灯具、窗帘、地毯、杯子/水杯、餐具等。
收纳包/收纳袋（软质有拉链） → 包；收纳盒/架（硬质） → 家居
11. 戒指_项链_耳钉_手链
戒指、项链、耳钉、耳环、手链、手镯、脚链、传统装饰手表等首饰珠宝类。
胸针 → 发饰饰品
12. 帽子
棒球帽、毛线帽、遮阳帽、贝雷帽、渔夫帽等一切帽类。
13. 内衣
文胸、内裤、睡衣、家居服、塑身衣、袜子、泳衣/比基尼等贴身衣物。
外穿型背心 → 服饰_上衣
14. 手机壳
手机壳、手机保护套、手机膜等手机保护配件。
平板保护壳 → 电子
15. 鞋子
运动鞋、皮鞋、拖鞋、凉鞋、靴子、高跟鞋等一切鞋类。
鞋垫 → 杂物
16. 眼镜
近视眼镜、太阳镜、防蓝光眼镜、老花镜、墨镜等。
眼镜盒 → 杂物；VR眼镜 → 电子
17. 杂物
以上分类均不符合时选择此项。

输出要求：
先进行简要分析（2-3句话），然后输出最终分类。格式如下：
分析：1688标题中包含关键词"XXX"，指向【XX】类；图片显示商品为XXX，确认属于【XX】类。
分类：XXX

⚠️ 绝对规则（违反即错误）：
1. 当1688标题中包含明确品类词时，必须以标题为最高优先级，严禁忽略标题
2. 标题含"裤/短裤/阔腿裤/打底裤"→ 必须归入 服饰_裤子
3. 标题含"连衣裙/长裙/旗袍"→ 必须归入 服饰_连衣裙
4. 标题含"衬衫/T恤/卫衣/外套/夹克/毛衣/马甲/棉服/风衣/sweater/shirt/jacket/coat/pullover"→ 必须归入 服饰_上衣
5. 标题含"鞋/靴/拖鞋/凉鞋/shoe/boot/sandal"→ 必须归入 鞋子
6. 标题含"帽/hat/cap"→ 必须归入 帽子
7. 标题含"睡衣/睡袍/家居服/内裤/文胸/袜/泳衣/比基尼/pajama"→ 必须归入 内衣
8. 标题含"摆件/夜灯/杯/水杯/lamp/bottle/cup/figurine"→ 必须归入 家居
9. 标题含"手机壳/手机套/phone case"→ 必须归入 手机壳
10. 标题含"眼镜/太阳镜/墨镜/sunglasses"→ 必须归入 眼镜
11. 禁止将明确有品类词的商品归入"包"或"杂物"，除非标题真的在说包/杂物"""

# 合法分类集合（用于结果校验）
VALID_CATEGORIES = {
    "包", "保健穿戴", "电子", "发饰饰品",
    "服饰_裤子", "服饰_连衣裙", "服饰_上衣",
    "挂件", "化妆品", "家居", "戒指_项链_耳钉_手链",
    "帽子", "内衣", "手机壳", "鞋子", "眼镜", "杂物",
}


def clean_result(text: str) -> str:
    """清理AI回复中的think标签，提取分类名"""
    # 去掉think标签
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    # 提取answer标签
    answer_match = re.search(r'<answer>(.*?)</answer>', text, re.DOTALL)
    if answer_match:
        text = answer_match.group(1).strip()

    # 核心提取：正则匹配"分类：XXX"或"分类:XXX"
    # 兼容格式：分类：鞋子 / 分类: 鞋子 / **分类：**鞋子 / 分类：**鞋子**
    patterns = [
        r'分类[：:]\s*\**\s*([^\n\*]+)',      # 分类：鞋子 或 分类：**鞋子**
        r'最终分类[：:]\s*\**\s*([^\n\*]+)',   # 最终分类：鞋子
        r'归[类为入][：:]\s*\**\s*([^\n\*]+)', # 归类：鞋子
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            category_text = m.group(1).strip().strip('*').strip('【').strip('】').strip()
            # 从提取的文本中匹配合法分类
            for cat in VALID_CATEGORIES:
                if cat in category_text:
                    return cat
            # 如果提取到的文本本身就是合法分类
            if category_text in VALID_CATEGORIES:
                return category_text

    # 兜底：从整个文本中查找合法分类名（取最后出现的）
    last_cat = None
    last_pos = -1
    for cat in VALID_CATEGORIES:
        pos = text.rfind(cat)
        if pos > last_pos:
            last_pos = pos
            last_cat = cat
    if last_cat:
        return last_cat

    return text.strip()


def exponential_backoff_retry(max_retries=10, base_wait_time=2, max_wait_time=60):
    def decorator(func):
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    result = await func(*args, **kwargs)
                    if isinstance(result, str) and '分析失败' in result:
                        raise Exception(f"AI分析失败: {result}")
                    return result
                except Exception as e:
                    logger.error(f"第{attempt+1}/{max_retries}次尝试失败: {str(e)[:200]}")
                    if attempt == max_retries - 1:
                        raise Exception(f"AI识别失败（重试{max_retries}次后）: {str(e)}")
                    wait_time = min(base_wait_time * (2 ** attempt), max_wait_time)
                    logger.warning(f"等待{wait_time}秒后重试...")
                    await asyncio.sleep(wait_time)
        return async_wrapper
    return decorator


class ZhipuAIKeyManager:
    def __init__(self, key_blacklist_duration=180):
        self.key_blacklist_duration = key_blacklist_duration
        self.key_blacklist = {}
        self.key_rotation_index = -1
        self._keys_cache: List[str] = []
        self._lock = threading.RLock()
        self.consecutive_failures = {}

    def fetch_keys(self, api_url='http://47.95.157.46:8520/api/zhipuai_key', max_retries=3) -> List[str]:
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        data = parse.urlencode({}, True)
        proxies = {'http': None, 'https': None}
        for attempt in range(1, max_retries + 1):
            try:
                logger.info(f"正在获取ZhipuAI密钥 (第{attempt}/{max_retries}次)...")
                response = requests.post(api_url, headers=headers, data=data, timeout=10, proxies=proxies)
                if response.status_code == 200:
                    result = response.json()
                    if result.get("success") and "data" in result:
                        keys = [item["key"] for item in result["data"]]
                        logger.info(f"成功获取 {len(keys)} 个密钥")
                        with self._lock:
                            self._keys_cache = keys
                        return keys
            except Exception as e:
                logger.error(f"密钥获取异常: {e}")
                if attempt < max_retries:
                    time.sleep(2 ** attempt)
        return []

    def get_available_key(self) -> Optional[str]:
        with self._lock:
            if not self._keys_cache:
                self.fetch_keys()
            self._cleanup_expired_blacklist()
            available_keys = [k for k in self._keys_cache if not self._is_key_blacklisted(k)]
            if not available_keys:
                return None
            self.key_rotation_index = (self.key_rotation_index + 1) % len(available_keys)
            return available_keys[self.key_rotation_index]

    def _is_key_blacklisted(self, api_key: str) -> bool:
        if api_key not in self.key_blacklist:
            return False
        if time.time() >= self.key_blacklist[api_key]:
            del self.key_blacklist[api_key]
            return False
        return True

    def _cleanup_expired_blacklist(self):
        expired = [k for k, t in self.key_blacklist.items() if time.time() >= t]
        for k in expired:
            del self.key_blacklist[k]

    def add_to_blacklist(self, api_key: str, reason='调用失败', duration=None):
        with self._lock:
            d = duration if duration is not None else self.key_blacklist_duration
            self.key_blacklist[api_key] = time.time() + d
            logger.warning(f"密钥加入黑名单 ({reason}): ...{api_key[-8:]} ({d}秒)")

    def record_success(self, api_key: str):
        with self._lock:
            self.consecutive_failures[api_key] = 0

    def record_failure(self, api_key: str, error_msg=''):
        with self._lock:
            self.consecutive_failures[api_key] = self.consecutive_failures.get(api_key, 0) + 1
            error_lower = error_msg.lower()
            if any(kw in error_lower for kw in ['429', 'rate limit', '限流', 'concurrent', 'quota']):
                self.add_to_blacklist(api_key, "限流", 180)
            elif any(kw in error_lower for kw in ['401', 'unauthorized', 'invalid key', '密钥无效']):
                self.add_to_blacklist(api_key, "密钥无效", 3600)
            elif self.consecutive_failures[api_key] >= 5:
                self.add_to_blacklist(api_key, "连续失败", 180)


class ZhipuAIVision:
    API_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    DEFAULT_MODEL = "glm-4.1v-thinking-flash"

    def __init__(self):
        self.model = self.DEFAULT_MODEL
        self.key_manager = ZhipuAIKeyManager()
        self.key_manager.fetch_keys()
        self._session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=False)
            self._session = aiohttp.ClientSession(connector=connector, trust_env=False)
        return self._session

    # ── 标题矛盾检测规则 ──
    # 如果AI分类为key，但标题包含value中的任何关键词 → 矛盾
    CONTRADICTION_RULES = {
        "包": [
            "裤", "短裤", "阔腿裤", "打底裤", "牛仔裤", "西装裤",
            "鞋", "靴", "拖鞋", "凉鞋",
            "衬衫", "T恤", "卫衣", "外套", "夹克", "毛衣", "马甲", "棉服", "风衣", "皮衣",
            "连衣裙", "长裙", "旗袍",
            "帽",
            "睡衣", "睡袍", "内裤", "文胸", "袜",
            "摆件", "夜灯", "杯", "水杯", "水壶", "灯",
            "眼镜", "太阳镜", "墨镜",
            "耳机", "充电", "数据线",
            "手机壳", "手机套",
            "sweater", "shirt", "jacket", "coat", "dress", "shoe", "boot",
            "pants", "hat", "pullover", "sneaker",
            "bottle", "cup", "lamp", "figurine",
        ],
        "化妆品": [
            "摆件", "夜灯", "杯", "水杯", "灯", "雕像",
            "裤", "鞋", "靴", "衣", "裙", "帽",
            "lamp", "bottle", "cup", "figurine",
        ],
        "杂物": [
            "裤", "短裤", "阔腿裤", "打底裤",
            "鞋", "靴", "拖鞋", "凉鞋",
            "衬衫", "T恤", "卫衣", "外套", "夹克", "毛衣", "棉服",
            "连衣裙", "长裙",
            "帽",
            "耳机", "充电", "手机壳",
            "sweater", "shirt", "jacket", "shoe", "boot", "dress",
        ],
        "家居": [
            "睡衣", "睡袍", "家居服", "内裤", "文胸", "袜", "泳衣", "比基尼",
            "pajama", "sleepwear", "bikini",
        ],
        "电子": [
            "手机壳", "手机套", "phone case",
        ],
    }

    # 标题中有这些词时，直接强制覆盖分类（最后一道防线）
    FORCED_OVERRIDES = [
        # (标题关键词列表, 强制分类, 排除的原始分类)
        (["睡衣", "睡袍", "家居服套装", "pajama", "sleepwear"], "内衣", {"内衣"}),
        (["手机壳", "手机套", "phone case"], "手机壳", {"手机壳"}),
    ]

    async def classify(self, image_url: str, title: str = "") -> str:
        """
        分类流程：
        1. AI投票5次，取≥3票的结果
        2. 矛盾自检：分类结果与标题关键词是否冲突
        3. 冲突则重新投票一轮（带警告提示词）
        4. 强制覆盖：极端明显的标题关键词
        """
        title_lower = (title or "").lower()

        # 构建基础提示词
        if title:
            base_prompt = CLASSIFICATION_PROMPT_TEMPLATE.replace("{title_1688}", title)
        else:
            base_prompt = CLASSIFICATION_PROMPT_TEMPLATE.replace("{title_1688}", "（无标题）")

        # ── 第一轮：5票投票 ──
        winner = await self._vote_round(image_url, base_prompt, num_votes=5, round_label="主投票")

        # ── 矛盾自检 ──
        if title and self._check_contradiction(winner, title_lower):
            logger.warning(f"  ⚠ 矛盾检测：分类'{winner}' vs 标题'{title[:40]}'，启动纠正投票...")

            # 带警告的纠正提示词
            warning_prompt = base_prompt + f"""

⚠️ 纠正警告：
之前有AI将此商品分类为"{winner}"，但标题"{title}"中包含明显不属于"{winner}"的关键词。
请你特别仔细地重新分析标题中的品类关键词，标题关键词优先级最高。
绝对不要再分类为"{winner}"，除非你100%确定标题描述的确实是{winner}类商品。"""

            # 纠正投票5次
            corrected = await self._vote_round(image_url, warning_prompt, num_votes=5, round_label="纠正投票")
            logger.info(f"  纠正投票结果: {winner} → {corrected}")
            winner = corrected

        # ── 强制覆盖（最后防线） ──
        if title:
            for keywords, forced_cat, exclude_set in self.FORCED_OVERRIDES:
                if winner not in exclude_set:
                    for kw in keywords:
                        if kw in title_lower:
                            logger.warning(f"  ⚠ 强制覆盖: '{winner}' → '{forced_cat}' (标题含'{kw}')")
                            winner = forced_cat
                            break
                    if winner == forced_cat:
                        break

        return winner

    async def _vote_round(self, image_url: str, prompt: str, num_votes: int = 5, round_label: str = "") -> str:
        """执行一轮投票，返回获胜分类"""
        from collections import Counter

        MAX_ROUNDS = 3  # 最多重开3轮

        for attempt in range(1, MAX_ROUNDS + 1):
            if attempt > 1:
                logger.info(f"  {round_label}重开第{attempt}轮...")
            votes = []

            for i in range(num_votes):
                try:
                    raw = await self._recognize_with_retry(image_url, prompt)
                    cat = self._extract_category(raw)
                    votes.append(cat)
                    logger.info(f"  {round_label}第{attempt}轮第{i+1}票: {cat}")
                except Exception as e:
                    logger.warning(f"  {round_label}第{attempt}轮第{i+1}票失败: {e}")
                    votes.append("杂物")

            counter = Counter(votes)
            winner, count = counter.most_common(1)[0]
            majority = (num_votes // 2) + 1  # 5票需要3票，3票需要2票

            if count >= majority:
                logger.info(f"  ✅ {round_label}结果: {votes} → {winner}（{count}/{num_votes}票）")
                return winner
            else:
                logger.warning(f"  {round_label}无多数: {dict(counter)}，重开...")

        # 所有轮次都没收敛，取最高票
        logger.warning(f"  {round_label}未收敛，取最高票: {winner}")
        return winner

    def _check_contradiction(self, category: str, title_lower: str) -> bool:
        """检查分类结果是否与标题矛盾"""
        keywords = self.CONTRADICTION_RULES.get(category)
        if not keywords:
            return False
        for kw in keywords:
            if kw.lower() in title_lower:
                logger.info(f"  矛盾检测命中: 分类'{category}' vs 标题关键词'{kw}'")
                return True
        return False

    def _extract_category(self, raw_result: str) -> str:
        """从AI原始回复中提取分类名"""
        category = clean_result(raw_result)
        if category in VALID_CATEGORIES:
            return category
        for cat in VALID_CATEGORIES:
            if cat in category:
                return cat
        return "杂物"

    @exponential_backoff_retry(max_retries=10, base_wait_time=2, max_wait_time=60)
    async def _recognize_with_retry(self, image_url: str, prompt: str) -> str:
        return await self._do_recognize(image_url, prompt)

    async def _do_recognize(self, image_url: str, prompt: str) -> str:
        for round_num in range(50):
            for attempt in range(10):
                selected_key = self.key_manager.get_available_key()
                if not selected_key:
                    logger.warning("所有密钥都在黑名单中，等待30秒...")
                    await asyncio.sleep(30)
                    continue
                try:
                    headers = {
                        "Authorization": f"Bearer {selected_key}",
                        "Content-Type": "application/json",
                    }
                    payload = {
                        "model": self.model,
                        "temperature": 0.0,
                        "max_tokens": 300,
                        "messages": [{
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": image_url}},
                                {"type": "text", "text": prompt}
                            ]
                        }]
                    }
                    session = await self._get_session()
                    async with session.post(self.API_URL, headers=headers, json=payload, timeout=120) as response:
                        if response.status == 200:
                            result = await response.json()
                            content = result['choices'][0]['message']['content']
                            if content:
                                self.key_manager.record_success(selected_key)
                                return content
                            raise Exception("响应content为空")
                        else:
                            text = await response.text()
                            raise Exception(f"HTTP {response.status}: {text}")
                except Exception as e:
                    error_msg = str(e)
                    logger.error(f"第{round_num+1}轮第{attempt+1}次失败: {error_msg}")
                    self.key_manager.record_failure(selected_key, error_msg)
                    wait = 5 if '429' in error_msg else 1
                    await asyncio.sleep(wait)
        return "分析失败：达到最大重试次数"

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


def classify_image_sync(image_url: str, title: str = "") -> str:
    """同步接口，供主循环调用"""
    async def _run():
        vision = ZhipuAIVision()
        try:
            return await vision.classify(image_url, title)
        finally:
            await vision.close()
    return asyncio.run(_run())
