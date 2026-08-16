#!/usr/bin/env python3
"""Pixoo64 工作状态看板 v2

网页切换“请勿打扰 / 可以找我”，Pixoo64 轮流播放当前状态下的多套
高饱和动画图案。请勿打扰使用蓝色系（不用红色），可以找我使用绿色系
搭配彩虹点缀。

运行: python3 status_server.py
"""

import base64
import colorsys
import json
import math
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# 设备 IP 在网页端配置，这里只作为首次启动的可选初始值
PIXOO_IP = os.environ.get("PIXOO_IP", "")
PORT = int(os.environ.get("STATUS_PORT", "8000"))
ROTATE_SECONDS = int(os.environ.get("STATUS_ROTATE", "15"))
# 网页“检查更新”按钮转发到 Watchtower 的 HTTP API（Docker 部署时配置）
UPDATE_URL = os.environ.get("UPDATE_URL", "")
UPDATE_TOKEN = os.environ.get("UPDATE_TOKEN", "")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.environ.get(
    "STATE_FILE", os.path.join(BASE_DIR, "status.json"))
PREVIEW_FILE = os.environ.get(
    "PREVIEW_FILE", os.path.join(BASE_DIR, "status_preview.png"))

SCALE = 8
W = H = 64
CW = W * SCALE  # 512x512 工作画布

FONT_ZH = next(
    (p for p in (
        os.path.join(BASE_DIR, "fonts", "ZCOOLKuaiLe-Regular.ttf"),
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
    ) if os.path.exists(p)),
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
)

VALID_STATUSES = ("busy", "free")
STATUS_TEXT = {"busy": "请勿打扰", "free": "可以找我"}

_lock = threading.Lock()
_state = {
    "status": "free",
    "device_ok": True,
    "pattern": 0,
    "device_ip": PIXOO_IP,
}
_gif_counter = 100


# ---------------------------------------------------------------- 画布工具
def _hsv(h, s=0.9, v=0.95):
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return int(r * 255), int(g * 255), int(b * 255)


def _rainbow(t, s=0.9, v=0.98):
    """彩虹色，但完全避开红色：橙→黄→绿→青→蓝→紫。"""
    return _hsv(0.08 + (t % 1.0) * 0.75, s, v)


def _bg_gradient(top, mid, bot, glow):
    bg = Image.new("RGB", (CW, CW))
    d = ImageDraw.Draw(bg)
    for y in range(CW):
        t = y / (CW - 1)
        if t < 0.55:
            f = t / 0.55
            c = tuple(int(top[i] + (mid[i] - top[i]) * f) for i in range(3))
        else:
            f = (t - 0.55) / 0.45
            c = tuple(int(mid[i] + (bot[i] - mid[i]) * f) for i in range(3))
        d.line([(0, y), (CW, y)], fill=c)
    if glow:
        g = Image.new("RGBA", (CW, CW), (0, 0, 0, 0))
        gd = ImageDraw.Draw(g)
        gd.ellipse([CW * 0.14, CW * 0.12, CW * 0.86, CW * 0.88], fill=glow)
        g = g.filter(ImageFilter.GaussianBlur(70))
        bg = Image.alpha_composite(bg.convert("RGBA"), g)
    return bg


def _bg_rainbow_rows(hue_shift=0.0):
    bg = Image.new("RGBA", (CW, CW), (0, 0, 0, 255))
    d = ImageDraw.Draw(bg)
    for y in range(CW):
        d.line([(0, y), (CW, y)], fill=_rainbow(hue_shift + y / CW * 0.55, 0.85, 0.98))
    return bg


def _bg_stripes(hue_shift=0.0, stripe=64):
    bg = Image.new("RGBA", (CW, CW), (0, 0, 0, 255))
    d = ImageDraw.Draw(bg)
    colors = [_rainbow(hue_shift + i / 6, 0.9, 0.98) for i in range(6)]
    for i in range(-CW, CW * 2, stripe):
        d.line([(i, 0), (i + CW, CW)], fill=colors[(i // stripe) % 6], width=stripe)
    return bg


def _text_layer(text, font, fill, glow_fill, y_center, blur=18, stroke=0,
                stroke_fill=(0, 0, 0, 220)):
    layer = Image.new("RGBA", (CW, CW), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    bbox = ld.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (CW - w) // 2 - bbox[0]
    y = y_center - h // 2 - bbox[1]
    if glow_fill:
        glow = Image.new("RGBA", (CW, CW), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        gd.text((x, y), text, font=font, fill=glow_fill)
        glow = glow.filter(ImageFilter.GaussianBlur(blur))
        layer = Image.alpha_composite(layer, glow)
    ld = ImageDraw.Draw(layer)
    if stroke:
        ld.text((x, y), text, font=font, fill=fill,
                stroke_width=stroke, stroke_fill=stroke_fill)
    else:
        ld.text((x, y), text, font=font, fill=fill)
    return layer


def _frame_brackets(canvas, color, length=58, width=9, margin=16):
    d = ImageDraw.Draw(canvas)
    m = margin
    r = CW - margin
    b = CW - margin
    for x0, y0, dx, dy in (
        (m, m, 1, 1), (r - length, m, 1, 1), (m, b - length, 1, 1),
        (r - length, b - length, 1, 1),
    ):
        d.line([(x0, y0), (x0 + dx * length, y0)], fill=color, width=width)
        d.line([(x0, y0), (x0, y0 + dy * length)], fill=color, width=width)


def _heart(d, cx, cy, s, color):
    d.ellipse([cx - s, cy - s, cx, cy], fill=color)
    d.ellipse([cx, cy - s, cx + s, cy], fill=color)
    d.polygon([(cx - s - s // 2, cy - s // 3), (cx + s + s // 2, cy - s // 3),
               (cx, cy + s + s // 2)], fill=color)


def _star(d, cx, cy, r, color, inner=None):
    d.polygon(
        [(cx, cy - r), (cx + r // 4, cy - r // 4), (cx + r, cy),
         (cx + r // 4, cy + r // 4), (cx, cy + r), (cx - r // 4, cy + r // 4),
         (cx - r, cy), (cx - r // 4, cy - r // 4)],
        fill=color,
    )
    if inner:
        r2 = r // 2
        d.polygon(
            [(cx, cy - r2), (cx + r2 // 3, cy - r2 // 3), (cx + r2, cy),
             (cx + r2 // 3, cy + r2 // 3), (cx, cy + r2), (cx - r2 // 3, cy + r2 // 3),
             (cx - r2, cy), (cx - r2 // 3, cy - r2 // 3)],
            fill=inner,
        )


def _glow_layer(draw_cb, blur=14):
    g = Image.new("RGBA", (CW, CW), (0, 0, 0, 0))
    draw_cb(ImageDraw.Draw(g))
    return g.filter(ImageFilter.GaussianBlur(blur))


def _final(img):
    return img.convert("RGB").resize((W, H), Image.LANCZOS)


# ---------------------------------------------------------------- 蓝色系：请勿打扰
def gen_busy_ring():
    """蓝色禁止圆环，脉动 + 文字。"""
    frames = []
    for k in range(4):
        canvas = _bg_gradient((16, 26, 96), (8, 14, 60), (3, 5, 26), (70, 150, 255, 100))
        cx, cy = CW // 2, int(CW * 0.28)
        r = int(CW * 0.15) + (10 if k % 2 else 0)
        glow = _glow_layer(
            lambda d, cx=cx, cy=cy, r=r: d.ellipse(
                [cx - r, cy - r, cx + r, cy + r], outline=(40, 200, 255, 160), width=18),
            blur=14,
        )
        canvas.alpha_composite(glow)
        d = ImageDraw.Draw(canvas)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(70, 220, 255), width=11)
        d.line([(cx - r + 14, cy + r - 14), (cx + r - 14, cy - r + 14)],
               fill=(70, 220, 255), width=15)
        d.ellipse([cx - 9, cy - 9, cx + 9, cy + 9], fill=(255, 255, 255))
        font = ImageFont.truetype(FONT_ZH, 118)
        canvas.alpha_composite(_text_layer(
            "请勿打扰", font, (235, 248, 255), (30, 150, 255, 220),
            int(CW * 0.74), blur=20, stroke=7, stroke_fill=(3, 8, 34, 255)))
        frames.append(_final(canvas))
    return frames


def gen_busy_waves():
    """蓝色波纹流动 + 文字。"""
    frames = []
    colors = [(20, 170, 255), (60, 220, 255), (120, 150, 255)]
    for k in range(6):
        canvas = _bg_gradient((5, 12, 46), (10, 24, 78), (3, 7, 30), (30, 140, 255, 90))
        d = ImageDraw.Draw(canvas)
        for band, color in enumerate(colors):
            base = 70 + band * 62
            for x in range(CW):
                y = base + int(24 * math.sin((x + k * 42) / 38.0 + band * 1.7))
                d.rectangle([x, y, x + 1, y + 5], fill=color)
        font = ImageFont.truetype(FONT_ZH, 118)
        canvas.alpha_composite(_text_layer(
            "请勿打扰", font, (255, 255, 255), (40, 160, 255, 180),
            int(CW * 0.68), blur=16, stroke=7, stroke_fill=(3, 8, 34, 255)))
        frames.append(_final(canvas))
    return frames


def gen_busy_neon():
    """霓虹蓝文字 + 闪烁角框。"""
    frames = []
    for k in range(4):
        canvas = Image.new("RGBA", (CW, CW), (2, 5, 20, 255))
        font = ImageFont.truetype(FONT_ZH, 116)
        canvas.alpha_composite(_text_layer(
            "请勿打扰", font, (140, 245, 255), (0, 170, 255, 220),
            int(CW * 0.46), blur=26, stroke=2, stroke_fill=(0, 40, 90, 255)))
        on = k % 2 == 0
        _frame_brackets(canvas, (0, 225, 255) if on else (0, 110, 190), width=10)
        d = ImageDraw.Draw(canvas)
        half = int(150 * (1 - 0.22 * k))
        d.line([(CW // 2 - half, 420), (CW // 2 + half, 420)],
               fill=(0, 210, 255), width=9)
        frames.append(_final(canvas))
    return frames


# ---------------------------------------------------------------- 绿色系：可以找我
def gen_free_smiley():
    """彩虹斜纹背景 + 眨眼笑脸。"""
    frames = []
    for k in range(4):
        canvas = _bg_stripes(hue_shift=k * 0.02)
        d = ImageDraw.Draw(canvas)
        cx, cy, r = CW // 2, int(CW * 0.30), int(CW * 0.17)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(30, 20, 10), width=9)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 236, 90))
        e = r // 5
        for ex in (cx - r // 3, cx + r // 3):
            if k == 1:  # 闭眼
                d.line([ex - e, cy - r // 3, ex + e, cy - r // 3],
                       fill=(30, 20, 10), width=7)
            else:
                d.ellipse([ex - e, cy - r // 3 - e, ex + e, cy - r // 3 + e],
                          fill=(30, 20, 10))
        d.arc([cx - r // 2, cy - r // 3, cx + r // 2, cy + r // 1.5],
              start=20, end=160, fill=(30, 20, 10), width=9)
        font = ImageFont.truetype(FONT_ZH, 104)
        canvas.alpha_composite(_text_layer(
            "可以找我", font, (255, 255, 255), (0, 200, 120, 200),
            int(CW * 0.78), blur=16, stroke=7, stroke_fill=(10, 30, 20, 255)))
        frames.append(_final(canvas))
    return frames


def gen_free_hearts():
    """绿色渐变背景 + 漂浮爱心。"""
    frames = []
    hearts = [
        (int(CW * 0.16), 26, (255, 170, 40)),
        (int(CW * 0.50), 20, (180, 100, 255)),
        (int(CW * 0.84), 30, (255, 220, 60)),
    ]
    for k in range(6):
        canvas = _bg_gradient((10, 130, 80), (6, 80, 60), (3, 40, 34), (60, 255, 150, 80))
        d = ImageDraw.Draw(canvas)
        for x, s, color in hearts:
            y = (CW * 0.32 - k * 45) % (CW * 0.60) + CW * 0.10
            _heart(d, x, int(y), s, color)
        for sx, sy, sr in ((70, 300, 7), (450, 330, 6), (150, 420, 5), (390, 420, 6)):
            _star(d, sx, sy, sr, (255, 255, 255))
        font = ImageFont.truetype(FONT_ZH, 104)
        canvas.alpha_composite(_text_layer(
            "可以找我", font, (255, 255, 255), (40, 220, 120, 180),
            int(CW * 0.80), blur=15, stroke=7, stroke_fill=(6, 30, 24, 255)))
        frames.append(_final(canvas))
    return frames


def gen_free_star():
    """彩虹渐变背景 + 脉动大星星。"""
    frames = []
    for k in range(6):
        canvas = _bg_rainbow_rows(hue_shift=k * 0.03)
        cx, cy = CW // 2, int(CW * 0.28)
        r = int(CW * 0.16) + (8 if k % 2 else 0)
        glow = _glow_layer(
            lambda d, cx=cx, cy=cy, r=r: _star(d, cx, cy, r + 14, (255, 220, 60, 150)),
            blur=16,
        )
        canvas.alpha_composite(glow)
        d = ImageDraw.Draw(canvas)
        _star(d, cx, cy, r, (255, 250, 90), inner=(120, 240, 255))
        font = ImageFont.truetype(FONT_ZH, 104)
        canvas.alpha_composite(_text_layer(
            "可以找我", font, (255, 255, 255), (40, 180, 120, 200),
            int(CW * 0.78), blur=16, stroke=7, stroke_fill=(20, 20, 30, 255)))
        frames.append(_final(canvas))
    return frames


# ---------------------------------------------------------------- 图案注册
PATTERNS = {
    "busy": [
        ("蓝色禁止环", gen_busy_ring, 280),
        ("蓝色波纹", gen_busy_waves, 180),
        ("霓虹蓝字", gen_busy_neon, 240),
    ],
    "free": [
        ("彩虹笑脸", gen_free_smiley, 320),
        ("漂浮爱心", gen_free_hearts, 220),
        ("星光脉动", gen_free_star, 240),
    ],
}


# ---------------------------------------------------------------- Pixoo 推送
def _http_post_json(url, obj, timeout=4):
    req = urllib.request.Request(
        url, data=json.dumps(obj).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _current_ip():
    with _lock:
        return (_state.get("device_ip") or "").strip()


def _next_gif_id():
    global _gif_counter
    ip = _current_ip()
    if not ip:
        raise RuntimeError("未配置 Pixoo 设备 IP，请在网页上设置")
    try:
        resp = _http_post_json(f"http://{ip}/post", {"Command": "Draw/GetHttpGifId"})
        if resp.get("error_code") == 0:
            return int(resp["PicId"])
    except Exception:
        pass
    _gif_counter += 1
    return _gif_counter


def push_animation(frames, speed):
    """多帧动画逐帧推送给设备；失败时整体重试一次。"""
    last_err = None
    for attempt in range(2):
        try:
            ip = _current_ip()
            if not ip:
                raise RuntimeError("未配置 Pixoo 设备 IP，请在网页上设置")
            pic_id = _next_gif_id()
            for i, frame in enumerate(frames):
                resp = _http_post_json(f"http://{ip}/post", {
                    "Command": "Draw/SendHttpGif",
                    "PicNum": len(frames),
                    "PicWidth": W,
                    "PicOffset": i,
                    "PicID": pic_id,
                    "PicSpeed": speed,
                    "PicData": base64.b64encode(frame.tobytes()).decode(),
                })
                if resp.get("error_code") != 0:
                    raise RuntimeError(f"device error: {resp}")
            return True
        except Exception as e:
            last_err = e
            time.sleep(0.3)
    raise RuntimeError(f"push failed: {last_err}")


# ---------------------------------------------------------------- 状态管理
def load_state():
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        if data.get("status") in VALID_STATUSES:
            _state["status"] = data["status"]
        _state["pattern"] = int(data.get("pattern", 0))
        if data.get("device_ip"):
            _state["device_ip"] = str(data["device_ip"]).strip()
    except Exception:
        pass


def save_state():
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(_state, f, ensure_ascii=False, indent=2)


def pattern_info(status):
    pats = PATTERNS[status]
    idx = _state["pattern"] % len(pats)
    return {"name": pats[idx][0], "index": idx, "total": len(pats)}


def push_pattern(status, index):
    """生成并推送指定状态的第 index 套图案。"""
    pats = PATTERNS[status]
    index %= len(pats)
    name, gen, speed = pats[index]
    frames = gen()
    ok = push_animation(frames, speed)
    with _lock:
        _state["status"] = status
        _state["pattern"] = index
        _state["device_ok"] = ok
    save_preview(frames[0])
    save_state()
    return ok, name


def apply_status(status):
    """切换状态并立即推送该状态的第一套图案。"""
    return push_pattern(status, 0)


def apply_device_ip(ip):
    """保存设备 IP 并立即推送当前状态验证连通性。"""
    ip = ip.strip()
    with _lock:
        _state["device_ip"] = ip
    save_state()
    if not ip:
        with _lock:
            _state["device_ok"] = False
        save_state()
        return False, "IP 不能为空"
    with _lock:
        status = _state["status"]
        index = _state["pattern"]
    try:
        ok, name = push_pattern(status, index)
        return ok, None
    except Exception as e:
        with _lock:
            _state["device_ok"] = False
        save_state()
        return False, str(e)


def trigger_update():
    """把网页“检查更新”按钮转发给 Watchtower 的 HTTP API。"""
    if not UPDATE_URL:
        return {"enabled": False, "message": "未启用更新按钮（仅 Docker 部署支持）"}
    try:
        req = urllib.request.Request(
            UPDATE_URL,
            method="POST",
            headers={"Authorization": f"Bearer {UPDATE_TOKEN}"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read().decode("utf-8", "replace")
            return {"enabled": True, "status": r.status, "result": body}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        return {"enabled": True, "status": e.code, "result": body}
    except Exception as e:
        return {"enabled": True, "status": 0, "result": str(e)}


def save_preview(img):
    os.makedirs(os.path.dirname(PREVIEW_FILE), exist_ok=True)
    img.resize((W * SCALE, H * SCALE), Image.NEAREST).save(PREVIEW_FILE)


def _rotator():
    """每 ROTATE_SECONDS 秒切到下一套图案。"""
    while True:
        time.sleep(ROTATE_SECONDS)
        with _lock:
            status = _state["status"]
            index = (_state["pattern"] + 1) % len(PATTERNS[status])
        try:
            ok, name = push_pattern(status, index)
            print(f"[轮播] {STATUS_TEXT[status]} -> {name} ({'ok' if ok else 'fail'})")
        except Exception as e:
            with _lock:
                _state["device_ok"] = False
            save_state()
            print(f"[轮播] 推送失败: {e}")


# ---------------------------------------------------------------- Web 页面
HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>工作状态 · Pixoo</title>
<style>
  :root {
    --bg: #0d0f1c; --panel: #171a2e; --line: #2a2f4e;
    --busy: #3b82f6; --busy-dim: #173a8f; --free: #22d37e; --free-dim: #0e6b40;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
    background: radial-gradient(1200px 600px at 50% -10%, #1b2140, var(--bg));
    color: #eef1ff; font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
    padding: 24px;
  }
  .card {
    width: 100%; max-width: 640px; background: var(--panel); border: 1px solid var(--line);
    border-radius: 24px; padding: 28px; box-shadow: 0 24px 60px rgba(0,0,0,.45);
  }
  h1 { margin: 0 0 4px; font-size: 22px; letter-spacing: 1px; }
  .sub { color: #8b91b4; font-size: 13px; margin-bottom: 20px; }
  .row { display: flex; gap: 14px; margin: 18px 0; }
  button {
    flex: 1; border: 0; border-radius: 16px; padding: 18px 10px; cursor: pointer;
    font-size: 17px; font-weight: 700; color: #fff; letter-spacing: 2px;
    transition: transform .12s ease, filter .12s ease; font-family: inherit;
  }
  button:active { transform: scale(.96); }
  button.busy { background: linear-gradient(160deg, var(--busy), var(--busy-dim)); }
  button.free { background: linear-gradient(160deg, var(--free), var(--free-dim)); }
  .settings { display: flex; gap: 10px; margin: 16px 0 4px; }
  .settings input {
    flex: 1; min-width: 0; background: #0e1122; border: 1px solid var(--line);
    border-radius: 10px; color: #eef1ff; padding: 9px 12px; font-size: 14px;
    font-family: inherit; outline: none;
  }
  .settings input:focus { border-color: #4a5aa8; }
  button.small {
    flex: 0 0 auto; border: 1px solid var(--line); background: #232946;
    color: #cdd3f5; border-radius: 10px; padding: 9px 14px; cursor: pointer;
    font-size: 13px; font-family: inherit; transition: filter .12s ease;
  }
  button.small:hover { filter: brightness(1.25); }
  .update-msg { color: #8b91b4; font-size: 12px; min-height: 16px; margin: 2px 0 10px; }
  .status-line { display: flex; align-items: center; gap: 10px; font-size: 14px; }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: #666; }
  .dot.busy { background: var(--busy); box-shadow: 0 0 12px var(--busy); }
  .dot.free { background: var(--free); box-shadow: 0 0 12px var(--free); }
  .dot.offline { background: #666; }
  .pattern { color: #aeb4d8; font-size: 13px; margin-top: 8px; }
  .preview { margin-top: 14px; text-align: center; }
  .preview img {
    width: 256px; image-rendering: pixelated; border-radius: 12px;
    border: 1px solid var(--line); background: #000;
  }
  .meta { color: #8b91b4; font-size: 12px; margin-top: 14px; text-align: center; }
</style>
</head>
<body>
<div class="card">
  <h1>工作状态</h1>
  <div class="sub">当前状态会轮流播放多套图案显示在 Pixoo64 上</div>
  <div class="status-line">
    <span class="dot" id="dot"></span>
    <span id="label">加载中…</span>
    <span style="flex:1"></span>
    <span id="device" class="sub" style="margin:0"></span>
  </div>
  <div class="pattern" id="pattern"></div>
  <div class="row">
    <button class="busy" onclick="setStatus('busy')">请勿打扰</button>
    <button class="free" onclick="setStatus('free')">可以找我</button>
  </div>
  <div class="settings">
    <input id="ip" placeholder="Pixoo 设备 IP，例如 192.168.1.100" spellcheck="false">
    <button class="small" onclick="saveIp()">保存设备</button>
    <button class="small" id="updBtn" onclick="checkUpdate()" style="display:none">检查更新</button>
  </div>
  <div class="update-msg" id="updMsg"></div>
  <div class="preview"><img id="preview" src="/preview.png" alt="Pixoo 预览"></div>
  <div class="meta">图案每 15 秒自动轮换 · 局域网内任意设备都能修改状态 · NAS 每 12 小时自动检查更新</div>
</div>
<script>
  let cur = null;
  async function refresh() {
    try {
      const r = await fetch('/api/status');
      const s = await r.json();
      cur = s.status;
      const dot = document.getElementById('dot');
      dot.className = 'dot ' + (s.device_ok ? cur : 'offline');
      if (!s.device_ip) {
        document.getElementById('label').textContent = '未配置设备 IP';
        document.getElementById('device').textContent = '请在下方填写';
      } else {
        document.getElementById('label').textContent =
          cur === 'busy' ? '请勿打扰' : '可以找我';
        document.getElementById('device').textContent =
          s.device_ok ? 'Pixoo 已同步' : 'Pixoo 离线';
      }
      const p = s.pattern;
      document.getElementById('pattern').textContent =
        '当前图案：' + p.name + '（' + (p.index + 1) + '/' + p.total + '）';
      const ipEl = document.getElementById('ip');
      if (document.activeElement !== ipEl) ipEl.value = s.device_ip || '';
      document.getElementById('updBtn').style.display =
        s.update_enabled ? '' : 'none';
      const pv = document.getElementById('preview');
      pv.src = '/preview.png?t=' + Date.now();
    } catch (e) { /* ignore */ }
  }
  async function saveIp() {
    const ip = document.getElementById('ip').value.trim();
    const msg = document.getElementById('updMsg');
    if (!ip) { msg.textContent = '请填写设备 IP'; return; }
    try {
      const r = await fetch('/api/device', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ip: ip})
      });
      const s = await r.json();
      msg.textContent = s.ok ? '设备已保存并同步' : '保存失败：' + (s.error || '无法连接设备');
    } catch (e) { msg.textContent = '请求失败'; }
    refresh();
  }
  async function checkUpdate() {
    const msg = document.getElementById('updMsg');
    msg.textContent = '正在检查更新…';
    try {
      const r = await fetch('/api/update', {method: 'POST'});
      const s = await r.json();
      if (s.enabled && s.result) {
        try {
          const j = JSON.parse(s.result);
          msg.textContent = (j.message || j.error || '已检查') + (s.status ? '（' + s.status + '）' : '');
        } catch (e) {
          msg.textContent = s.result;
        }
      } else {
        msg.textContent = s.message || '未启用更新按钮';
      }
    } catch (e) { msg.textContent = '请求失败'; }
  }
  async function setStatus(st) {
    if (st === cur) return;
    try {
      await fetch('/api/status', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({status: st})
      });
    } catch (e) {}
    refresh();
  }
  setInterval(refresh, 3000);
  refresh();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[web]", self.address_string(), fmt % args)

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        with _lock:
            status = _state["status"]
            device_ok = _state["device_ok"]
            device_ip = _state.get("device_ip", "")
        if self.path in ("/", "/index.html"):
            self._send(200, HTML_PAGE, "text/html; charset=utf-8")
        elif self.path.startswith("/preview.png"):
            try:
                with open(PREVIEW_FILE, "rb") as f:
                    self._send(200, f.read(), "image/png")
            except OSError:
                self._send(404, "preview not ready")
        elif self.path == "/api/status":
            self._send(200, json.dumps({
                "status": status, "device_ok": device_ok,
                "pattern": pattern_info(status),
                "device_ip": device_ip,
                "update_enabled": bool(UPDATE_URL),
            }))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._send(400, json.dumps({"error": "bad json"}))
            return

        if self.path == "/api/status":
            status = req.get("status")
            if status not in VALID_STATUSES:
                self._send(400, json.dumps({"error": "status must be busy or free"}))
                return
            try:
                ok, name = apply_status(status)
            except Exception as e:
                ok, name = False, None
                with _lock:
                    _state["device_ok"] = False
                save_state()
            with _lock:
                device_ok = _state["device_ok"]
            self._send(200, json.dumps({
                "ok": ok, "status": status, "device_ok": device_ok,
                "pattern": pattern_info(status),
            }))
        elif self.path == "/api/device":
            ip = req.get("ip", "")
            ok, err = apply_device_ip(ip)
            with _lock:
                device_ok = _state["device_ok"]
            self._send(200, json.dumps({
                "ok": ok, "device_ok": device_ok,
                "device_ip": ip.strip(), "error": err,
            }))
        elif self.path == "/api/update":
            self._send(200, json.dumps(trigger_update()))
        else:
            self._send(404, json.dumps({"error": "not found"}))


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("1.1.1.1", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    load_state()
    print("Pixoo 状态看板 v2")
    with _lock:
        status = _state["status"]
        device_ip = _state.get("device_ip", "")
    if device_ip:
        print(f"  设备:  {device_ip}")
        try:
            ok, name = apply_status(status)
            print(f"  同步:  {STATUS_TEXT[status]} - {name} {'成功' if ok else '失败'}")
        except Exception as e:
            print(f"  同步:  失败 - {e}")
            with _lock:
                _state["device_ok"] = False
    else:
        print("  设备:  未配置（请在网页上填写 Pixoo 设备 IP）")
        with _lock:
            _state["device_ok"] = False
    threading.Thread(target=_rotator, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"  本机:  http://127.0.0.1:{PORT}")
    print(f"  局域网: http://{lan_ip()}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
