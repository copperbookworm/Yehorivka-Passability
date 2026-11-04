# -*- coding: utf-8 -*-
"""DeepSeek 识图：截图 -> 网格坐标。

要点（实测得出，别随手改）：
- 模型 id 是 deepseek-flash（DeepSeek-V4.1-Flash，支持图片输入）。
- 必须先吐 reasoning_content，max_tokens 给小了 content 会是空的 -> 400。
- 不加 reasoning_effort，加了更慢且不准。
"""
from __future__ import annotations

import base64
import io
import json
import re
import time
import urllib.error
import urllib.request

from PIL import ImageGrab, Image

API_BASE = "https://api.deepseek.com"
MODEL = "deepseek-flash"

SYSTEM_PROMPT = (
    "你是军事游戏截图读数字工具。图中通常有一行标题 CURRENT GRID，其下一行是网格坐标，"
    "形如 D4-5-7 或 L13-3-3（1 个字母 + 1~2 位数字 - 1 位数字 - 1 位数字）。"
    "只输出坐标本身：字母大写，三段用短横连接，无空格。图中没有可读坐标时只输出 NONE。"
)

COORD_RE = re.compile(r"[A-Z]\s*\d{1,2}\s*-\s*\d\s*-\s*\d")


class VisionError(RuntimeError):
    pass


def grab(region: tuple[int, int, int, int]) -> Image.Image:
    """region = (x, y, w, h)，物理像素，全屏坐标。"""
    x, y, w, h = region
    if w < 4 or h < 4:
        raise VisionError("识别区域太小，请重新框选")
    img = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
    return img.convert("RGB")


def _payload(img) -> dict:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return {
        "model": MODEL,
        "max_tokens": 400,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "读出网格坐标。"},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
                ],
            },
        ],
    }


def parse_reply(text: str) -> str | None:
    """从模型回复里抠出规范坐标；没有则 None。"""
    if not text:
        return None
    for m in COORD_RE.finditer(text.upper()):
        raw = m.group(0)
        parts = [p.strip() for p in raw.split("-")]
        if len(parts) != 3:
            continue
        letter, row, a, b = parts[0][0], parts[0][1:], parts[1], parts[2]
        return f"{letter.upper()}{int(row)}-{int(a)}-{int(b)}"
    return None


def ask(img, api_key: str, timeout: float = 30.0) -> tuple[str | None, str, int]:
    """返回 (坐标 或 None, 原始回复, 毫秒)。"""
    if not api_key:
        raise VisionError("还没有填 DeepSeek 密钥")
    body = json.dumps(_payload(img)).encode("utf-8")
    req = urllib.request.Request(
        API_BASE + "/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
        },
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise VisionError(f"接口返回 {e.code}：{detail}") from None
    except urllib.error.URLError as e:
        raise VisionError(f"连不上 api.deepseek.com：{e.reason}") from None
    ms = int((time.time() - t0) * 1000)
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError):
        raise VisionError("接口返回格式异常：" + json.dumps(data, ensure_ascii=False)[:300]) from None
    content = (msg.get("content") or "").strip()
    if not content:
        raise VisionError("模型没给出结果（content 为空），请重试")
    return parse_reply(content), content, ms
