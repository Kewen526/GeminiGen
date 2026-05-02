# -*- coding: utf-8 -*-
"""上传用户文件并返回可访问 URL"""

import os
import uuid
import logging

from fastapi import UploadFile

logger = logging.getLogger(__name__)

# cos_upload 在上层目录
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cos_upload import upload_to_cos


async def save_upload_to_url(upload: UploadFile, temp_dir: str) -> str:
    """保存上传文件到临时目录，上传到图床，返回公网 URL"""
    ext      = os.path.splitext(upload.filename or "")[1] or ".png"
    filename = f"upload_{uuid.uuid4().hex}{ext}"
    local    = os.path.join(temp_dir, filename)

    content = await upload.read()
    with open(local, "wb") as f:
        f.write(content)

    try:
        url = upload_to_cos(local, f"platform_uploads/{filename}")
        if url:
            return url
        # 兜底：返回本地路径（worker 在同机器时可直接读）
        return local
    except Exception as e:
        logger.warning(f"upload_to_url 失败，使用本地路径: {e}")
        return local
    finally:
        # 文件保留，worker 处理完后清理
        pass
