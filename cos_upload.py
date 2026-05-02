# -*- coding: utf-8 -*-
"""
图片上传模块
优先级：360图床（第1次）→ 360图床（第2次重试）→ 腾讯云COS兜底
"""

import logging
import requests
from qcloud_cos import CosConfig, CosS3Client

logger = logging.getLogger(__name__)

# ============================================================
# 腾讯云 COS 配置（兜底）
# ============================================================
SECRET_ID  = 'AKIDrYz93g26vUmpb6KHxMULvFI4aonVw60d'
SECRET_KEY = 'OVMwFH1astc4FApEMCm47tOcaGfnfFXZ'
REGION     = 'ap-beijing'
BUCKET     = 'ceshi-1300392622'


def _upload_to_360(local_path: str) -> str | None:
    """上传到360图床，成功返回URL，失败返回None。"""
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
            logger.info(f"✅ 360图床上传成功: {url}")
            return url
        else:
            logger.warning(f"⚠ 360图床返回失败: {j.get('error')}")
            return None
    except Exception as e:
        logger.warning(f"⚠ 360图床上传异常: {e}")
        return None


def _upload_to_cos(local_path: str, filename: str) -> str | None:
    """上传到腾讯云COS，成功返回URL，失败返回None。"""
    try:
        config = CosConfig(
            Region=REGION,
            SecretId=SECRET_ID,
            SecretKey=SECRET_KEY,
            Token=None,
            Proxies={'http': None, 'https': None},
        )
        client = CosS3Client(config)
        client.put_object_from_local_file(
            Bucket=BUCKET,
            LocalFilePath=local_path,
            Key=filename,
        )
        url = f"https://{BUCKET}.cos.{REGION}.myqcloud.com/{filename}"
        logger.info(f"✅ COS兜底上传成功: {url}")
        return url
    except Exception as e:
        logger.error(f"❌ COS兜底上传失败: {e}")
        return None


def upload_to_cos(local_path: str, filename: str) -> str | None:
    """
    统一上传入口（对外接口，保持原有调用方式不变）。
    优先360图床（最多2次），失败后兜底COS。

    参数：
        local_path : 本地文件绝对路径
        filename   : COS Key，仅在兜底时使用
    """
    # 第1次：360图床
    logger.info("☁️ 尝试上传到360图床（第1次）...")
    url = _upload_to_360(local_path)
    if url:
        return url

    # 第2次：360图床重试
    logger.warning("⚠ 360图床第1次失败，重试第2次...")
    url = _upload_to_360(local_path)
    if url:
        return url

    # 兜底：腾讯云COS
    logger.warning("⚠ 360图床两次均失败，降级到腾讯云COS兜底...")
    return _upload_to_cos(local_path, filename)