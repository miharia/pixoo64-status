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
    # 当前显示模式：pattern=状态图案 | countdown=倒计时 | stopwatch=秒表 | scoreboard=比分板
    "mode": "pattern",
    # 滚动文字通知：{"text","color","speed","duration","until","sent"}
    "notify": None,
    # 倒计时：{"total","until"}
    "countdown": None,
    "stopwatch_running": False,
    "scoreboard": {"blue": 0, "red": 0},
    # 设备控制设置（网页手动模式）
    "settings": {
        "brightness": 100,
        "screen_on": True,
        "rotation": 0,
        "mirror": False,
        "white_balance": [255, 255, 255],
        "high_light": False,
        "hour_mode": 24,
        "temp_unit": 0,
    },
    # 记录用户在网页上动过哪些设置（避免启动时覆盖 App 里配好的值）
    "settings_touched": [],
    # 自动亮度 / 夜间关屏
    "auto": {
        "enabled": False,
        "day_brightness": 100,
        "night_brightness": 20,
        "night_start": "22:00",
        "night_end": "08:00",
        "night_off": False,
    },
}
_gif_counter = 100
_text_counter = 0
# 已成功应用到设备上的设置（避免重复下发）
_applied_settings = {}


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


def _clamp(v, lo, hi):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return lo


def _pixoo_command(variants, timeout=4, ip=None):
    """按候选列表依次尝试设备命令，返回第一个 error_code==0 的响应。

    不同固件版本对同一功能的命令名/参数写法不同（例如 Tools/SetTimer 与
    Tool/SetCountDown），这里把几种写法都试一遍，兼容新旧固件。
    """
    last = None
    for command, params in variants:
        try:
            obj = {"Command": command}
            if params:
                obj.update(params)
            resp = _http_post_json(
                f"http://{ip or _current_ip()}/post", obj, timeout=timeout
            )
            if resp.get("error_code") == 0:
                return resp
            last = resp
        except Exception as e:
            last = e
    if isinstance(last, Exception):
        raise last
    raise RuntimeError(f"device error: {last}")


def _cmd_retry(variants, tries=3, delay=0.6, timeout=4):
    """设备对紧跟动画的命令偶尔会静默丢弃/无响应，幂等命令多试几次。"""
    last = None
    for i in range(tries):
        try:
            return _pixoo_command(variants, timeout=timeout)
        except Exception as e:
            last = e
            if i < tries - 1:
                time.sleep(delay)
    raise last


def _is_night(start, end):
    """判断当前时间是否落在夜间区间（支持跨零点，如 22:00-08:00）。"""
    def to_min(s):
        try:
            h, m = str(s).split(":")
            return int(h) * 60 + int(m)
        except Exception:
            return 22 * 60
    now = time.localtime()
    cur = now.tm_hour * 60 + now.tm_min
    s0, e0 = to_min(start), to_min(end)
    if s0 <= e0:
        return s0 <= cur < e0
    return cur >= s0 or cur < e0


# 可回读验证的设置：命令参数键 -> Channel/GetAllConf 里的字段
_VERIFY = {
    "brightness": ("Brightness", "Brightness"),
    "screen_on": ("OnOff", "LightSwitch"),
    "rotation": ("Mode", "GyrateAngle"),
    "mirror": ("Mode", "MirrorFlag"),
    "hour_mode": ("Mode", "Time24Flag"),
    "temp_unit": ("Mode", "TemperatureMode"),
}


def _apply_settings(force=False):
    """把网页上配置过的设备设置推给 Pixoo；自动亮度/夜间关屏也会在这里生效。"""
    with _lock:
        ip = (_state.get("device_ip") or "").strip()
        settings = dict(_state.get("settings", {}))
        auto = dict(_state.get("auto", {}))
        touched = set(_state.get("settings_touched", []))
    if not ip:
        return ["device_ip"]
    if auto.get("enabled"):
        night = _is_night(auto.get("night_start", "22:00"), auto.get("night_end", "08:00"))
        if night and auto.get("night_off"):
            settings["screen_on"] = False
        else:
            settings["screen_on"] = True
            settings["brightness"] = (
                int(auto.get("night_brightness", 20)) if night
                else int(auto.get("day_brightness", 100))
            )
        touched |= {"brightness", "screen_on"}

    wb = settings.get("white_balance", [255, 255, 255])
    rotation_deg = _clamp(settings.get("rotation", 0), 0, 270)
    specs = []

    def add(key, variants):
        if force or key in touched:
            specs.append((key, variants))

    add("brightness", [(
        "Channel/SetBrightness",
        {"Brightness": _clamp(settings.get("brightness", 100), 0, 100)},
    )])
    add("screen_on", [(
        "Channel/OnOffScreen",
        {"OnOff": 1 if settings.get("screen_on", True) else 0},
    )])
    add("rotation", [
        ("Device/SetScreenRotationAngle", {"Mode": rotation_deg // 90}),
        ("Device/SetRotationAngle", {"RotateAngle": rotation_deg}),
    ])
    add("mirror", [
        ("Device/SetMirrorMode", {"Mode": 1 if settings.get("mirror") else 0}),
        ("Device/SetMirrorMode", {"Mirror": 1 if settings.get("mirror") else 0}),
    ])
    add("white_balance", [
        ("Device/SetWhiteBalance", {
            "RValue": _clamp(wb[0], 0, 255),
            "GValue": _clamp(wb[1], 0, 255),
            "BValue": _clamp(wb[2], 0, 255),
        }),
        ("Device/SetWhiteBalance", {
            "WhiteBalanceR": _clamp(wb[0], 0, 255),
            "WhiteBalanceG": _clamp(wb[1], 0, 255),
            "WhiteBalanceB": _clamp(wb[2], 0, 255),
        }),
    ])
    add("high_light", [(
        "Device/SetHighLightMode",
        {"Mode": 1 if settings.get("high_light") else 0},
    )])
    add("hour_mode", [
        ("Device/SetTime24Flag", {"Mode": 1 if settings.get("hour_mode") == 24 else 0}),
        ("Device/SetHourMode", {"HourMode": 24 if settings.get("hour_mode") == 24 else 12}),
    ])
    add("temp_unit", [
        ("Device/SetDisTempMode", {"Mode": _clamp(settings.get("temp_unit", 0), 0, 1)}),
        ("Device/SetTempUnit", {"Unit": _clamp(settings.get("temp_unit", 0), 0, 1)}),
    ])

    failed = []
    for key, variants in specs:
        sig = f"{key}:{ip}"
        if not force and _applied_settings.get(sig) == variants:
            continue
        param_key = None
        conf_key = None
        if key in _VERIFY:
            param_key, conf_key = _VERIFY[key]
        want = variants[0][1].get(param_key) if param_key else None
        # 设备刚收完动画帧时偶尔会静默丢命令（返回 error_code 0 但没生效），
        # 所以下发后回读设备配置验证，不一致就稍等重试
        ok = False
        for attempt in range(4):
            try:
                _pixoo_command(variants, ip=ip)
                if param_key is not None:
                    time.sleep(0.4)
                    resp = _http_post_json(
                        f"http://{ip}/post", {"Command": "Channel/GetAllConf"})
                    if resp.get(conf_key) == want:
                        _applied_settings[sig] = variants
                        ok = True
                        break
                else:
                    _applied_settings[sig] = variants
                    ok = True
                    break
            except Exception:
                pass
            if attempt < 3:
                time.sleep(0.8)
        if not ok:
            failed.append(key)
    return failed


def _next_text_id():
    global _text_counter
    # 固件只接受很小的 TextId（实测 1001 会被拒绝），在 1-8 内循环使用
    _text_counter = _text_counter % 8 + 1
    return _text_counter


# ---------------------------------------------------------------- 新功能
def start_notify(text, color="#FFFFFF", speed=4, duration=10):
    """滚动文字通知：在屏幕上滚动显示一段时间，结束后自动恢复状态图案。"""
    text = str(text).strip()
    if not text:
        return False, "文字不能为空"
    with _lock:
        _state["notify"] = {
            "text": text,
            "color": color if str(color).startswith("#") else "#FFFFFF",
            "speed": _clamp(speed, 1, 20),
            "duration": _clamp(duration, 2, 3600),
            "until": time.time() + _clamp(duration, 2, 3600),
            "sent": False,
        }
    save_state()
    # 通知文字只在绘图模式生效：如果正在倒计时/秒表/比分板，先切回图案
    with _lock:
        mode = _state.get("mode", "pattern")
    if mode != "pattern":
        try:
            back_to_pattern()
        except Exception:
            pass
    # 立即尝试发送，失败也要如实告诉用户（后台仍会每秒自动重试）
    try:
        _cmd_retry([("Draw/SendHttpText", {
            "TextId": _next_text_id(),
            "x": 0, "y": 24, "dir": 1, "font": 4,
            "TextWidth": 64, "speed": _clamp(speed, 1, 20),
            "TextString": text,
            "color": color if str(color).startswith("#") else "#FFFFFF",
            "align": 1,
        })])
        with _lock:
            _state["notify"]["sent"] = True
        save_state()
        return True, None
    except Exception as e:
        return False, f"设备无响应，稍后自动重试：{e}"


def cancel_notify():
    with _lock:
        _state["notify"] = None
    save_state()
    try:
        _cmd_retry([("Draw/ClearHttpText", {})], tries=2)
    except Exception:
        pass


def start_countdown(seconds):
    """启动设备内置倒计时，结束后自动回到状态图案。"""
    seconds = _clamp(seconds, 1, 86400)
    with _lock:
        _state["mode"] = "countdown"
        _state["countdown"] = {"total": seconds, "until": time.time() + seconds}
    save_state()
    m, s = divmod(seconds, 60)
    _cmd_retry([
        ("Tools/SetTimer", {"Minute": m, "Second": s, "Status": 1}),
        ("Tool/SetCountDown", {"CountDownTime": seconds}),
    ])
    return True, None


def cancel_countdown():
    with _lock:
        _state["mode"] = "pattern"
        _state["countdown"] = None
    save_state()
    try:
        _cmd_retry([
            ("Tools/SetTimer", {"Minute": 0, "Second": 0, "Status": 0}),
            ("Tool/SetCountDown", {"CountDownTime": 0}),
        ], tries=2)
    except Exception:
        pass
    _restore_pattern()


def stopwatch_action(action):
    action = str(action).strip()
    with _lock:
        running = _state.get("stopwatch_running", False)
    if action == "start":
        _cmd_retry([
            ("Tools/SetStopWatch", {"Status": 1}),
            ("Tool/SetStopWatch", {"StopWatchStatus": 1}),
        ])
        with _lock:
            _state["stopwatch_running"] = True
            _state["mode"] = "stopwatch"
    elif action == "stop":
        _cmd_retry([
            ("Tools/SetStopWatch", {"Status": 0}),
            ("Tool/SetStopWatch", {"StopWatchStatus": 0}),
        ])
        with _lock:
            _state["stopwatch_running"] = False
            _state["mode"] = "stopwatch"
    elif action == "reset":
        try:
            _cmd_retry([("Tools/SetStopWatch", {"Status": 2})], tries=2)
        except Exception:
            pass
        try:
            _cmd_retry([("Tools/SetStopWatch", {"Status": 0})], tries=2)
        except Exception:
            pass
        with _lock:
            _state["stopwatch_running"] = False
            _state["mode"] = "stopwatch"
    else:
        return False, "action 必须是 start/stop/reset"
    save_state()
    return True, None


def set_scoreboard(blue, red):
    blue, red = _clamp(blue, 0, 999), _clamp(red, 0, 999)
    _cmd_retry([
        ("Tools/SetScoreBoard", {"BlueScore": blue, "RedScore": red}),
        ("Tool/SetScoreBoard", {"Blue": blue, "Red": red}),
    ])
    with _lock:
        _state["scoreboard"] = {"blue": blue, "red": red}
        _state["mode"] = "scoreboard"
    save_state()
    return True, None


def play_buzzer(on_ms=500, off_ms=500, total_ms=1500):
    """蜂鸣器响几声（固件每个 beep 约 50ms，过短的参数可能不响）。"""
    _cmd_retry([
        ("Device/PlayBuzzer", {
            "ActiveTime": _clamp(on_ms, 100, 60000),
            "OffTime": _clamp(off_ms, 100, 60000),
            "TotalTime": _clamp(total_ms, 100, 60000),
        }),
        ("Device/PlayTFGif", {
            "ActiveTimeInCycle": _clamp(on_ms, 100, 60000),
            "OffTimeInCycle": _clamp(off_ms, 100, 60000),
            "PlayTotalTime": _clamp(total_ms, 100, 60000),
        }),
    ])
    return True, None


def discover_devices():
    """通过 Divoom 云端发现同一局域网里的 Pixoo 设备。"""
    try:
        req = urllib.request.Request(
            "https://app.divoom-gz.com/Device/ReturnSameLANDevice",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"ok": False, "error": str(e)}
    code = data.get("error_code", data.get("ReturnCode"))
    if code not in (0, None):
        return {"ok": False, "error": f"error_code={code}"}
    devs = data.get("DeviceList") or []
    return {"ok": True, "devices": [
        {"name": d.get("DeviceName", ""), "ip": d.get("DevicePrivateIP", "")}
        for d in devs
    ]}


def get_weather():
    """读取设备内置天气（需要在 Divoom App 里先绑定天气城市）。"""
    try:
        resp = _pixoo_command([("Weather/GetWeather", {})])
        return {"ok": True, "data": resp}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def back_to_pattern():
    """从倒计时/秒表/比分板等工具页回到状态图案。"""
    with _lock:
        status = _state["status"]
        index = _state["pattern"]
        _state["mode"] = "pattern"
        _state["countdown"] = None
        _state["stopwatch_running"] = False
    save_state()
    return push_pattern(status, index)


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
            # 先重置 GIF 计数，防止固件连续接收 ~300 帧后卡死
            try:
                _http_post_json(
                    f"http://{ip}/post", {"Command": "Draw/ResetHttpGifId"})
            except Exception:
                pass
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
        for k in ("device_ok", "pattern", "device_ip", "mode", "notify",
                  "countdown", "stopwatch_running", "settings_touched"):
            if k in data:
                _state[k] = data[k]
        if isinstance(data.get("device_ip"), str):
            _state["device_ip"] = data["device_ip"].strip()
        if isinstance(data.get("settings"), dict):
            merged = dict(_state["settings"])
            merged.update(data["settings"])
            _state["settings"] = merged
        if isinstance(data.get("auto"), dict):
            merged = dict(_state["auto"])
            merged.update(data["auto"])
            _state["auto"] = merged
        if isinstance(data.get("scoreboard"), dict):
            merged = dict(_state["scoreboard"])
            merged.update(data["scoreboard"])
            _state["scoreboard"] = merged
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
    if ok:
        # 设备刚收完动画帧时容易丢紧随其后的命令，稍等让它消化
        time.sleep(0.3)
    with _lock:
        _state["status"] = status
        _state["pattern"] = index
        _state["device_ok"] = ok
    save_preview(frames[0])
    save_state()
    return ok, name


def apply_status(status):
    """切换状态并立即推送该状态的第一套图案。"""
    with _lock:
        _state["mode"] = "pattern"
        _state["notify"] = None
        _state["countdown"] = None
        _state["stopwatch_running"] = False
    save_state()
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
        try:
            _apply_settings()
        except Exception:
            pass
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


def _restore_pattern():
    with _lock:
        status = _state["status"]
        index = _state["pattern"]
    try:
        push_pattern(status, index)
        print(f"[恢复] 回到状态图案：{STATUS_TEXT[status]}")
    except Exception as e:
        with _lock:
            _state["device_ok"] = False
        save_state()
        print(f"[恢复] 推送失败: {e}")


def _main_loop():
    """1 秒一跳：处理滚动文字、倒计时结束、状态图案轮播。"""
    last_rotate = time.time()
    while True:
        time.sleep(1)
        now = time.time()
        with _lock:
            mode = _state.get("mode", "pattern")
            notify = _state.get("notify")
        try:
            if notify:
                if now >= notify.get("until", 0):
                    try:
                        _pixoo_command([("Draw/ClearHttpText", {})])
                    except Exception:
                        pass
                    with _lock:
                        _state["notify"] = None
                    save_state()
                    _restore_pattern()
                elif not notify.get("sent"):
                    _pixoo_command([("Draw/SendHttpText", {
                        "TextId": _next_text_id(),
                        "x": 0, "y": 24, "dir": 1, "font": 4,
                        "TextWidth": 64, "speed": notify.get("speed", 4),
                        "TextString": notify.get("text", ""),
                        "color": notify.get("color", "#FFFFFF"),
                        "align": 1,
                    })])
                    with _lock:
                        _state["notify"]["sent"] = True
                    save_state()
                continue
            if mode == "countdown":
                with _lock:
                    cd = _state.get("countdown")
                if cd and now >= cd["until"]:
                    with _lock:
                        _state["mode"] = "pattern"
                        _state["countdown"] = None
                    save_state()
                    _restore_pattern()
                continue
            if mode == "pattern" and now - last_rotate >= ROTATE_SECONDS:
                last_rotate = now
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
        except Exception:
            pass


def _settings_loop():
    """每 30 秒按网页配置/自动模式同步设备亮度、开关屏等设置。"""
    while True:
        time.sleep(30)
        try:
            _apply_settings()
        except Exception:
            pass


def public_state():
    """网页用到的完整状态（含设备控制、自动模式、工具页）。"""
    with _lock:
        d = {
            "status": _state["status"],
            "device_ok": _state.get("device_ok", True),
            "device_ip": _state.get("device_ip", ""),
            "mode": _state.get("mode", "pattern"),
            "notify": _state.get("notify"),
            "countdown": _state.get("countdown"),
            "stopwatch_running": _state.get("stopwatch_running", False),
            "scoreboard": _state.get("scoreboard", {"blue": 0, "red": 0}),
            "settings": dict(_state.get("settings", {})),
            "settings_touched": _state.get("settings_touched", []),
            "auto": dict(_state.get("auto", {})),
            "update_enabled": bool(UPDATE_URL),
        }
    auto = d["auto"]
    if auto.get("enabled"):
        night = _is_night(auto.get("night_start", "22:00"), auto.get("night_end", "08:00"))
        if night and auto.get("night_off"):
            d["settings"]["screen_on"] = False
        else:
            d["settings"]["screen_on"] = True
            d["settings"]["brightness"] = (
                int(auto.get("night_brightness", 20)) if night
                else int(auto.get("day_brightness", 100))
            )
    d["pattern"] = pattern_info(d["status"])
    return d


SETTING_KEYS = {
    "brightness", "screen_on", "rotation", "mirror", "white_balance",
    "high_light", "hour_mode", "temp_unit",
}
AUTO_KEYS = {
    "enabled", "day_brightness", "night_brightness",
    "night_start", "night_end", "night_off",
}


def update_config(settings=None, auto=None):
    """网页保存设备控制/自动模式配置，校验后立即向设备应用。"""
    with _lock:
        cur_s = _state.setdefault("settings", {})
        cur_a = _state.setdefault("auto", {})
        touched = set(_state.setdefault("settings_touched", []))
        if isinstance(settings, dict):
            for k, v in settings.items():
                if k not in SETTING_KEYS:
                    continue
                try:
                    if k == "brightness":
                        v = _clamp(v, 0, 100)
                    elif k == "rotation":
                        v = max(0, min(3, round(int(v) / 90))) * 90
                    elif k == "white_balance":
                        if not (isinstance(v, (list, tuple)) and len(v) == 3):
                            continue
                        v = [_clamp(x, 0, 255) for x in v]
                    elif k in ("mirror", "screen_on", "high_light"):
                        v = bool(v)
                    elif k == "hour_mode":
                        v = 24 if int(v) == 24 else 12
                    elif k == "temp_unit":
                        v = 1 if int(v) else 0
                except (TypeError, ValueError):
                    # 非法值直接忽略，不让整个请求 500
                    continue
                cur_s[k] = v
                touched.add(k)
        if isinstance(auto, dict):
            for k, v in auto.items():
                if k not in AUTO_KEYS:
                    continue
                try:
                    if k == "enabled":
                        v = bool(v)
                    elif k == "night_off":
                        v = bool(v)
                    elif k in ("day_brightness", "night_brightness"):
                        v = _clamp(v, 0, 100)
                    else:
                        v = str(v)
                except (TypeError, ValueError):
                    continue
                cur_a[k] = v
        _state["settings_touched"] = sorted(touched)
    save_state()
    try:
        failed = _apply_settings() or []
    except Exception as e:
        return public_state(), str(e)
    if "device_ip" in failed:
        return public_state(), "未配置 Pixoo 设备 IP"
    if failed:
        names = {
            "brightness": "亮度", "screen_on": "屏幕", "rotation": "旋转",
            "mirror": "镜像", "white_balance": "白平衡", "high_light": "高亮",
            "hour_mode": "小时制", "temp_unit": "温度单位",
        }
        return public_state(), "部分设置未生效：" + "、".join(names.get(k, k) for k in failed)
    return public_state(), None


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
    margin: 0; min-height: 100vh; display: flex; flex-direction: column; align-items: center;
    background: radial-gradient(1200px 600px at 50% -10%, #1b2140, var(--bg));
    color: #eef1ff; font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
    padding: 24px;
  }
  .card {
    width: 100%; max-width: 680px; background: var(--panel); border: 1px solid var(--line);
    border-radius: 24px; padding: 24px 26px; box-shadow: 0 24px 60px rgba(0,0,0,.45);
  }
  .card + .card { margin-top: 16px; }
  h1 { margin: 0 0 4px; font-size: 22px; letter-spacing: 1px; }
  .sub { color: #8b91b4; font-size: 13px; margin-bottom: 16px; }
  .row { display: flex; gap: 14px; margin: 16px 0; }
  button {
    flex: 1; border: 0; border-radius: 16px; padding: 18px 10px; cursor: pointer;
    font-size: 17px; font-weight: 700; color: #fff; letter-spacing: 2px;
    transition: transform .12s ease, filter .12s ease; font-family: inherit;
  }
  button:active { transform: scale(.96); }
  button.busy { background: linear-gradient(160deg, var(--busy), var(--busy-dim)); }
  button.free { background: linear-gradient(160deg, var(--free), var(--free-dim)); }
  .settings { display: flex; gap: 10px; margin: 14px 0 4px; flex-wrap: wrap; }
  .settings input {
    flex: 1; min-width: 160px; background: #0e1122; border: 1px solid var(--line);
    border-radius: 10px; color: #eef1ff; padding: 9px 12px; font-size: 14px;
    font-family: inherit; outline: none;
  }
  .settings input:focus { border-color: #4a5aa8; }
  button.small, .btn {
    flex: 0 0 auto; border: 1px solid var(--line); background: #232946;
    color: #cdd3f5; border-radius: 10px; padding: 9px 14px; cursor: pointer;
    font-size: 13px; font-family: inherit; transition: filter .12s ease;
  }
  button.small:hover, .btn:hover { filter: brightness(1.25); }
  .btn.primary { background: linear-gradient(160deg, #2f6bff, #1d3f9e); border-color: transparent; color: #fff; }
  .update-msg { color: #8b91b4; font-size: 12px; min-height: 16px; margin: 2px 0 8px; }
  .status-line { display: flex; align-items: center; gap: 10px; font-size: 14px; }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: #666; }
  .dot.busy { background: var(--busy); box-shadow: 0 0 12px var(--busy); }
  .dot.free { background: var(--free); box-shadow: 0 0 12px var(--free); }
  .dot.offline { background: #666; }
  .pattern { color: #aeb4d8; font-size: 13px; margin-top: 8px; }
  .preview { margin-top: 12px; text-align: center; }
  .preview img {
    width: 224px; image-rendering: pixelated; border-radius: 12px;
    border: 1px solid var(--line); background: #000;
  }
  .sec { border-top: 1px solid var(--line); margin-top: 16px; padding-top: 14px; }
  .sec-title { font-size: 15px; font-weight: 700; margin: 0 0 12px; color: #dfe4ff; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px 18px; }
  .field { display: flex; flex-direction: column; gap: 5px; }
  .field label { font-size: 12px; color: #8b91b4; }
  input[type=range] { width: 100%; accent-color: #4a5aa8; }
  input[type=text], input[type=time], input[type=number], select {
    background: #0e1122; border: 1px solid var(--line); border-radius: 10px;
    color: #eef1ff; padding: 8px 10px; font-size: 14px; font-family: inherit; outline: none;
  }
  input[type=number] { width: 76px; }
  .chk { display: flex; align-items: center; gap: 8px; font-size: 14px; }
  .chk input { width: 16px; height: 16px; accent-color: #4a5aa8; }
  .btnrow { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
  .hint { color: #8b91b4; font-size: 12px; margin-top: 8px; }
  .sb-row { display: flex; align-items: center; justify-content: space-around; margin-top: 6px; }
  .sb-team { text-align: center; }
  .sb-team .name { font-size: 13px; color: #8b91b4; }
  .sb-score { font-size: 26px; font-weight: 800; margin: 2px 0; }
  pre.wx {
    background: #0e1122; border: 1px solid var(--line); border-radius: 10px;
    padding: 10px; font-size: 12px; max-height: 170px; overflow: auto; margin-top: 10px;
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
  <div class="pattern">
    <span id="pattern"></span>
    <span id="modeStatus" style="color:#8b91b4;margin-left:10px"></span>
    <button class="small" id="modeBtn" onclick="backToPattern()" style="display:none;margin-left:8px">回到图案</button>
  </div>
  <div class="row">
    <button class="busy" onclick="setStatus('busy')">请勿打扰</button>
    <button class="free" onclick="setStatus('free')">可以找我</button>
  </div>
  <div class="settings">
    <input id="ip" placeholder="Pixoo 设备 IP，例如 192.168.1.100" spellcheck="false">
    <button class="small" onclick="saveIp()">保存设备</button>
    <button class="small" onclick="discover()">发现设备</button>
    <button class="small" id="updBtn" onclick="checkUpdate()" style="display:none">检查更新</button>
  </div>
  <div class="update-msg" id="updMsg"></div>
  <div class="preview"><img id="preview" src="/preview.png" alt="Pixoo 预览"></div>
  <div class="meta">图案自动轮换 · 局域网内任意设备都能修改 · NAS 每 12 小时自动检查更新</div>
</div>

<div class="card">
  <div class="sec-title">设备控制</div>
  <div class="grid">
    <div class="field">
      <label>亮度 <span id="briVal" style="color:#cdd3f5"></span></label>
      <input type="range" id="bri" min="0" max="100" value="100"
             oninput="document.getElementById('briVal').textContent=this.value+'%'"
             onchange="saveConfig({settings:{brightness:+this.value}})">
    </div>
    <div class="field">
      <label>屏幕</label>
      <button class="small" id="screenBtn" onclick="toggleScreen()">屏幕：开</button>
    </div>
    <div class="field">
      <label>旋转角度</label>
      <select id="rotSel" onchange="saveConfig({settings:{rotation:+this.value}})">
        <option value="0">0°</option>
        <option value="90">90°</option>
        <option value="180">180°</option>
        <option value="270">270°</option>
      </select>
    </div>
    <div class="field">
      <label>镜像模式</label>
      <label class="chk"><input type="checkbox" id="mirChk" onchange="saveConfig({settings:{mirror:this.checked}})"> 开启</label>
    </div>
    <div class="field">
      <label>白平衡 R/G/B <span id="wbVal" style="color:#cdd3f5"></span></label>
      <input type="range" id="wbR" min="0" max="255" value="255" oninput="wbLabel()" onchange="saveConfig({settings:{white_balance:wb()}})">
      <input type="range" id="wbG" min="0" max="255" value="255" oninput="wbLabel()" onchange="saveConfig({settings:{white_balance:wb()}})">
      <input type="range" id="wbB" min="0" max="255" value="255" oninput="wbLabel()" onchange="saveConfig({settings:{white_balance:wb()}})">
    </div>
    <div class="field">
      <label>高亮模式</label>
      <label class="chk"><input type="checkbox" id="hlChk" onchange="saveConfig({settings:{high_light:this.checked}})"> 开启</label>
    </div>
    <div class="field">
      <label>小时制</label>
      <select id="hourSel" onchange="saveConfig({settings:{hour_mode:+this.value}})">
        <option value="24">24 小时</option>
        <option value="12">12 小时</option>
      </select>
    </div>
    <div class="field">
      <label>温度单位</label>
      <select id="tempSel" onchange="saveConfig({settings:{temp_unit:+this.value}})">
        <option value="0">摄氏 ℃</option>
        <option value="1">华氏 ℉</option>
      </select>
    </div>
  </div>
</div>

<div class="card">
  <div class="sec-title">自动亮度 / 夜间关屏</div>
  <div class="grid">
    <div class="field">
      <label class="chk"><input type="checkbox" id="autoChk" onchange="saveAuto()"> 启用自动模式</label>
    </div>
    <div class="field"></div>
    <div class="field">
      <label>白天亮度 <span id="dayBriVal"></span></label>
      <input type="range" id="dayBri" min="0" max="100" value="100"
             oninput="document.getElementById('dayBriVal').textContent=this.value+'%'" onchange="saveAuto()">
    </div>
    <div class="field">
      <label>夜间亮度 <span id="nightBriVal"></span></label>
      <input type="range" id="nightBri" min="0" max="100" value="20"
             oninput="document.getElementById('nightBriVal').textContent=this.value+'%'" onchange="saveAuto()">
    </div>
    <div class="field">
      <label>夜间开始</label>
      <input type="time" id="nightStart" value="22:00" onchange="saveAuto()">
    </div>
    <div class="field">
      <label>夜间结束</label>
      <input type="time" id="nightEnd" value="08:00" onchange="saveAuto()">
    </div>
    <div class="field" style="grid-column:1/-1">
      <label class="chk"><input type="checkbox" id="nightOffChk" onchange="saveAuto()"> 夜间完全关闭屏幕</label>
    </div>
  </div>
  <div class="hint">自动模式开启后：白天用“白天亮度”，夜间用“夜间亮度”；勾选关闭屏幕则夜间直接熄屏，早上恢复。</div>
</div>

<div class="card">
  <div class="sec-title">滚动文字通知</div>
  <div class="grid">
    <div class="field" style="grid-column:1/-1">
      <label>通知文字</label>
      <input type="text" id="ntText" placeholder="例如：3 小时后回来" spellcheck="false">
    </div>
    <div class="field">
      <label>颜色</label>
      <input type="color" id="ntColor" value="#FFD94A" style="height:38px;padding:4px">
    </div>
    <div class="field">
      <label>滚动速度（1-20）</label>
      <input type="number" id="ntSpeed" value="5" min="1" max="20">
    </div>
    <div class="field">
      <label>持续时间（秒）</label>
      <input type="number" id="ntDur" value="15" min="2" max="3600">
    </div>
    <div class="field">
      <label>操作</label>
      <button class="btn primary" onclick="sendNotify()">发送通知</button>
    </div>
  </div>
  <div class="hint" id="ntStatus"></div>
</div>

<div class="card">
  <div class="sec-title">工具页（倒计时 / 秒表 / 比分板 / 蜂鸣器）</div>
  <div class="sec">
    <div class="sec-title" style="margin-top:0">倒计时</div>
    <div class="grid">
      <div class="field">
        <label>分钟</label>
        <input type="number" id="cdMin" value="5" min="0" max="1439">
      </div>
      <div class="field">
        <label>秒</label>
        <input type="number" id="cdSec" value="0" min="0" max="59">
      </div>
    </div>
    <div class="btnrow">
      <button class="btn primary" onclick="cdStart()">开始倒计时</button>
      <button class="btn" onclick="cdCancel()">取消</button>
    </div>
    <div class="hint" id="cdStatus"></div>
  </div>
  <div class="sec">
    <div class="sec-title" style="margin-top:0">秒表</div>
    <div class="btnrow">
      <button class="btn primary" onclick="swAction('start')">开始</button>
      <button class="btn" onclick="swAction('stop')">停止</button>
      <button class="btn" onclick="swAction('reset')">复位</button>
    </div>
    <div class="hint" id="swStatus"></div>
  </div>
  <div class="sec">
    <div class="sec-title" style="margin-top:0">比分板</div>
    <div class="sb-row">
      <div class="sb-team">
        <div class="name">红队</div>
        <div class="sb-score" id="sbRed">0</div>
        <button class="btn" onclick="sbAdj('red',1)">+1</button>
        <button class="btn" onclick="sbAdj('red',-1)">-1</button>
      </div>
      <div class="sb-team">
        <div class="name">蓝队</div>
        <div class="sb-score" id="sbBlue">0</div>
        <button class="btn" onclick="sbAdj('blue',1)">+1</button>
        <button class="btn" onclick="sbAdj('blue',-1)">-1</button>
      </div>
    </div>
    <div class="btnrow"><button class="btn" onclick="sbAdj('reset')">归零</button></div>
  </div>
  <div class="sec">
    <div class="sec-title" style="margin-top:0">蜂鸣器</div>
    <div class="btnrow">
      <button class="btn primary" onclick="bz(300,300,800)">短响</button>
      <button class="btn primary" onclick="bz(500,500,3000)">长响</button>
    </div>
    <div class="hint">蜂鸣器可能较响，注意别吓到人。</div>
  </div>
</div>

<div class="card">
  <div class="sec-title">天气（设备内置，需先在 Divoom App 绑定城市）</div>
  <button class="btn" onclick="refreshWeather()">刷新天气</button>
  <pre class="wx" id="wxBox">点击上方按钮获取天气数据</pre>
</div>

<script>
  let cur = null;
  let curScreen = true;
  async function post(path, body) {
    const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body||{})});
    return r.json();
  }
  function msg(t) { document.getElementById('updMsg').textContent = t; }
  function wb() {
    return [+document.getElementById('wbR').value, +document.getElementById('wbG').value, +document.getElementById('wbB').value];
  }
  function wbLabel() {
    const v = wb();
    document.getElementById('wbVal').textContent = v.join(' / ');
  }
  function fmt(s) { return JSON.stringify(s, null, 2); }
  async function refresh() {
    let s;
    try { s = await (await fetch('/api/status')).json(); } catch (e) { return; }
    cur = s.status;
    const dot = document.getElementById('dot');
    dot.className = 'dot ' + (s.device_ok ? cur : 'offline');
    if (!s.device_ip) {
      document.getElementById('label').textContent = '未配置设备 IP';
      document.getElementById('device').textContent = '请在下方填写';
    } else {
      document.getElementById('label').textContent = cur === 'busy' ? '请勿打扰' : '可以找我';
      document.getElementById('device').textContent = s.device_ok ? 'Pixoo 已同步' : 'Pixoo 离线';
    }
    document.getElementById('pattern').textContent =
      '当前图案：' + s.pattern.name + '（' + (s.pattern.index + 1) + '/' + s.pattern.total + '）';
    const mode = s.mode || 'pattern';
    const modeTxt = {pattern:'状态图案', countdown:'倒计时中', stopwatch:'秒表', scoreboard:'比分板'}[mode] || mode;
    document.getElementById('modeStatus').textContent = '· ' + modeTxt;
    document.getElementById('modeBtn').style.display = mode === 'pattern' ? 'none' : '';
    const ipEl = document.getElementById('ip');
    if (document.activeElement !== ipEl) ipEl.value = s.device_ip || '';
    document.getElementById('updBtn').style.display = s.update_enabled ? '' : 'none';
    document.getElementById('preview').src = '/preview.png?t=' + Date.now();

    // 正在操作控件时不覆盖表单值，避免拖动/选择到一半被刷回去
    const ae = document.activeElement;
    const ctlBusy = ae && (ae.tagName === 'INPUT' || ae.tagName === 'SELECT');
    if (!ctlBusy) {
      const st = s.settings || {};
      curScreen = st.screen_on !== false;
      document.getElementById('bri').value = st.brightness ?? 100;
      document.getElementById('briVal').textContent = (st.brightness ?? 100) + '%';
      document.getElementById('screenBtn').textContent = curScreen ? '屏幕：开' : '屏幕：关';
      document.getElementById('rotSel').value = st.rotation ?? 0;
      document.getElementById('mirChk').checked = !!st.mirror;
      const w = st.white_balance || [255,255,255];
      document.getElementById('wbR').value = w[0];
      document.getElementById('wbG').value = w[1];
      document.getElementById('wbB').value = w[2];
      wbLabel();
      document.getElementById('hlChk').checked = !!st.high_light;
      document.getElementById('hourSel').value = st.hour_mode ?? 24;
      document.getElementById('tempSel').value = st.temp_unit ?? 0;

      const au = s.auto || {};
      document.getElementById('autoChk').checked = !!au.enabled;
      document.getElementById('dayBri').value = au.day_brightness ?? 100;
      document.getElementById('dayBriVal').textContent = (au.day_brightness ?? 100) + '%';
      document.getElementById('nightBri').value = au.night_brightness ?? 20;
      document.getElementById('nightBriVal').textContent = (au.night_brightness ?? 20) + '%';
      document.getElementById('nightStart').value = au.night_start || '22:00';
      document.getElementById('nightEnd').value = au.night_end || '08:00';
      document.getElementById('nightOffChk').checked = !!au.night_off;
    }

    const nt = s.notify;
    document.getElementById('ntStatus').textContent = nt
      ? '通知中：' + nt.text + '（剩余 ' + Math.max(0, Math.ceil(nt.until - Date.now()/1000)) + ' 秒）'
      : '';
    document.getElementById('cdStatus').textContent = s.countdown
      ? '剩余 ' + Math.max(0, Math.ceil(s.countdown.until - Date.now()/1000)) + ' 秒'
      : '';
    document.getElementById('swStatus').textContent = s.stopwatch_running ? '运行中' : '已停止';
    const sb = s.scoreboard || {blue:0, red:0};
    document.getElementById('sbRed').textContent = sb.red;
    document.getElementById('sbBlue').textContent = sb.blue;
  }
  async function saveConfig(body) {
    try {
      const r = await post('/api/config', body || {});
      if (r && r.error) msg('设置失败：' + r.error);
      else msg('设置已应用');
    } catch (e) { msg('保存失败'); }
    refresh();
  }
  function saveAuto() {
    saveConfig({auto: {
      enabled: document.getElementById('autoChk').checked,
      day_brightness: +document.getElementById('dayBri').value,
      night_brightness: +document.getElementById('nightBri').value,
      night_start: document.getElementById('nightStart').value,
      night_end: document.getElementById('nightEnd').value,
      night_off: document.getElementById('nightOffChk').checked
    }});
  }
  async function toggleScreen() { saveConfig({settings:{screen_on: !curScreen}}); }
  async function sendNotify() {
    const text = document.getElementById('ntText').value.trim();
    if (!text) { msg('请填写通知文字'); return; }
    const r = await post('/api/notify', {
      text: text,
      color: document.getElementById('ntColor').value,
      speed: +document.getElementById('ntSpeed').value,
      duration: +document.getElementById('ntDur').value
    });
    msg(r.ok ? '通知已发送' : '发送失败：' + (r.error || ''));
    refresh();
  }
  async function cdStart() {
    const total = (+document.getElementById('cdMin').value) * 60 + (+document.getElementById('cdSec').value);
    if (total < 1) { msg('请设置倒计时时间'); return; }
    const r = await post('/api/countdown', {seconds: total});
    msg(r.ok ? '倒计时已开始' : '失败：' + (r.error || ''));
    refresh();
  }
  async function cdCancel() { await post('/api/countdown', {action:'cancel'}); refresh(); }
  async function swAction(action) {
    const r = await post('/api/stopwatch', {action: action});
    msg(r.ok ? '秒表已' + ({start:'开始', stop:'停止', reset:'复位'}[action] || '操作') : '失败：' + (r.error || ''));
    refresh();
  }
  async function sbAdj(team, delta) {
    let s;
    try { s = await (await fetch('/api/status')).json(); } catch (e) { return; }
    const sb = s.scoreboard || {blue:0, red:0};
    if (team === 'reset') {
      await post('/api/scoreboard', {blue:0, red:0});
    } else {
      sb[team] = Math.max(0, (sb[team] || 0) + delta);
      await post('/api/scoreboard', {blue: sb.blue, red: sb.red});
    }
    refresh();
  }
  async function bz(on, off, total) {
    const r = await post('/api/buzzer', {on_ms:on, off_ms:off, total_ms:total});
    msg(r.ok ? '蜂鸣器已响' : '失败：' + (r.error || ''));
  }
  async function discover() {
    msg('正在发现设备…');
    const r = await post('/api/discover', {});
    if (r.ok && r.devices && r.devices.length) {
      document.getElementById('ip').value = r.devices[0].ip;
      msg('发现 ' + r.devices.length + ' 台：' + r.devices.map(d => d.ip + (d.name ? '（' + d.name + '）' : '')).join('、'));
    } else {
      msg('未发现设备：' + (r.error || '无结果'));
    }
  }
  async function refreshWeather() {
    document.getElementById('wxBox').textContent = '加载中…';
    try {
      const j = await (await fetch('/api/weather')).json();
      document.getElementById('wxBox').textContent = fmt(j.data || j);
    } catch (e) {
      document.getElementById('wxBox').textContent = '请求失败';
    }
  }
  async function backToPattern() { await post('/api/mode', {mode:'pattern'}); refresh(); }
  async function saveIp() {
    const ip = document.getElementById('ip').value.trim();
    if (!ip) { msg('请填写设备 IP'); return; }
    try {
      const s = await post('/api/device', {ip: ip});
      msg(s.ok ? '设备已保存并同步' : '保存失败：' + (s.error || '无法连接设备'));
    } catch (e) { msg('请求失败'); }
    refresh();
  }
  async function checkUpdate() {
    msg('正在检查更新…');
    try {
      const s = await post('/api/update', {});
      if (s.enabled) {
        if (s.result) {
          try {
            const j = JSON.parse(s.result);
            msg((j.message || j.error || '已检查') + (s.status ? '（' + s.status + '）' : ''));
          } catch (e) { msg(s.result); }
        } else {
          msg('已检查更新' + (s.status ? '（' + s.status + '）' : ''));
        }
      } else {
        msg(s.message || '未启用更新按钮');
      }
    } catch (e) { msg('请求失败'); }
  }
  async function setStatus(st) {
    if (st === cur) return;
    try { await post('/api/status', {status: st}); } catch (e) {}
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
        if self.path in ("/", "/index.html"):
            self._send(200, HTML_PAGE, "text/html; charset=utf-8")
        elif self.path.startswith("/preview.png"):
            try:
                with open(PREVIEW_FILE, "rb") as f:
                    self._send(200, f.read(), "image/png")
            except OSError:
                self._send(404, "preview not ready")
        elif self.path == "/api/status":
            self._send(200, json.dumps(public_state()))
        elif self.path == "/api/weather":
            self._send(200, json.dumps(get_weather()))
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
        elif self.path == "/api/config":
            try:
                state, err = update_config(req.get("settings"), req.get("auto"))
            except Exception as e:
                state, err = public_state(), str(e)
            self._send(200, json.dumps({"ok": err is None, "error": err, "state": state}))
        elif self.path == "/api/notify":
            ok, err = start_notify(
                req.get("text", ""), req.get("color", "#FFFFFF"),
                req.get("speed", 5), req.get("duration", 15))
            self._send(200, json.dumps({"ok": ok, "error": err}))
        elif self.path == "/api/countdown":
            if req.get("action") == "cancel":
                cancel_countdown()
                self._send(200, json.dumps({"ok": True}))
            else:
                try:
                    ok, err = start_countdown(int(req.get("seconds", 0)))
                except Exception as e:
                    ok, err = False, str(e)
                self._send(200, json.dumps({"ok": ok, "error": err}))
        elif self.path == "/api/stopwatch":
            ok, err = stopwatch_action(req.get("action", ""))
            self._send(200, json.dumps({"ok": ok, "error": err}))
        elif self.path == "/api/scoreboard":
            try:
                ok, err = set_scoreboard(int(req.get("blue", 0)), int(req.get("red", 0)))
            except Exception as e:
                ok, err = False, str(e)
            self._send(200, json.dumps({"ok": ok, "error": err}))
        elif self.path == "/api/buzzer":
            try:
                ok, err = play_buzzer(
                    int(req.get("on_ms", 500)), int(req.get("off_ms", 500)),
                    int(req.get("total_ms", 1500)))
            except Exception as e:
                ok, err = False, str(e)
            self._send(200, json.dumps({"ok": ok, "error": err}))
        elif self.path == "/api/mode":
            if req.get("mode") == "pattern":
                try:
                    back_to_pattern()
                    self._send(200, json.dumps({"ok": True}))
                except Exception as e:
                    self._send(200, json.dumps({"ok": False, "error": str(e)}))
            else:
                self._send(400, json.dumps({"error": "mode must be pattern"}))
        elif self.path == "/api/discover":
            self._send(200, json.dumps(discover_devices()))
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
    print("Pixoo 状态看板 v3（多功能版）")
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
    threading.Thread(target=_main_loop, daemon=True).start()
    threading.Thread(target=_settings_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"  本机:  http://127.0.0.1:{PORT}")
    print(f"  局域网: http://{lan_ip()}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
