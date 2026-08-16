#!/usr/bin/env python3
"""Generate a designed 64x64 image and push it to the local Pixoo64."""

import base64
import json
import os
import urllib.request

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# 示例地址，请改成你设备实际的局域网 IP
DEVICE = os.environ.get("PIXOO_IP", "192.168.1.100")
FONT_PATH = "/System/Library/Fonts/Supplemental/Arial Rounded Bold.ttf"
SCALE = 8
W = H = 64
CW = W * SCALE  # 512x512 working canvas


def gradient_background() -> Image.Image:
    bg = Image.new("RGB", (CW, CW))
    d = ImageDraw.Draw(bg)
    top = (30, 18, 62)
    mid = (20, 13, 42)
    bot = (8, 8, 20)
    for y in range(CW):
        t = y / (CW - 1)
        if t < 0.55:
            f = t / 0.55
            c = tuple(int(top[i] + (mid[i] - top[i]) * f) for i in range(3))
        else:
            f = (t - 0.55) / 0.45
            c = tuple(int(mid[i] + (bot[i] - mid[i]) * f) for i in range(3))
        d.line([(0, y), (CW, y)], fill=c)

    # warm radial glow behind the text
    glow = Image.new("RGBA", (CW, CW), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([CW * 0.14, CW * 0.16, CW * 0.86, CW * 0.84], fill=(255, 120, 180, 70))
    glow = glow.filter(ImageFilter.GaussianBlur(70))
    return Image.alpha_composite(bg.convert("RGBA"), glow)


def text_layer(text: str, font: ImageFont.FreeTypeFont, fill, glow_fill) -> Image.Image:
    layer = Image.new("RGBA", (CW, CW), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    bbox = ld.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (CW - w) // 2 - bbox[0]
    y = (CW - h) // 2 - bbox[1]

    glow = Image.new("RGBA", (CW, CW), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.text((x, y), text, font=font, fill=glow_fill)
    glow = glow.filter(ImageFilter.GaussianBlur(16))
    layer = Image.alpha_composite(layer, glow)

    ld = ImageDraw.Draw(layer)
    ld.text((x, y), text, font=font, fill=fill)
    return layer


def sparkle(img: Image.Image, cx: int, cy: int, r: int, color) -> None:
    layer = Image.new("RGBA", (CW, CW), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.polygon(
        [(cx, cy - r), (cx + r // 4, cy - r // 4), (cx + r, cy),
         (cx + r // 4, cy + r // 4), (cx, cy + r), (cx - r // 4, cy + r // 4),
         (cx - r, cy), (cx - r // 4, cy - r // 4)],
        fill=color,
    )
    layer = layer.filter(ImageFilter.GaussianBlur(2))
    img.alpha_composite(layer)


def heart(img: Image.Image, cx: int, cy: int, s: int, color) -> None:
    d = ImageDraw.Draw(img)
    d.ellipse([cx - s, cy - s, cx, cy], fill=color)
    d.ellipse([cx, cy - s, cx + s, cy], fill=color)
    d.polygon([(cx - s - s // 2, cy - s // 3), (cx + s + s // 2, cy - s // 3),
               (cx, cy + s + s // 2)], fill=color)


def build_image() -> Image.Image:
    canvas = gradient_background()

    font_hello = ImageFont.truetype(FONT_PATH, 165)
    font_nancy = ImageFont.truetype(FONT_PATH, 165)

    hello = text_layer("hello", font_hello, (255, 214, 165), (255, 92, 140))
    nancy = text_layer("nancy", font_nancy, (150, 240, 255), (64, 156, 255))
    canvas.alpha_composite(hello, (0, -int(CW * 0.155)))
    canvas.alpha_composite(nancy, (0, int(CW * 0.155)))

    # decorative sparkles and heart
    sparkle(canvas, int(CW * 0.09), int(CW * 0.13), 10, (255, 255, 220))
    sparkle(canvas, int(CW * 0.90), int(CW * 0.22), 8, (255, 220, 255))
    sparkle(canvas, int(CW * 0.12), int(CW * 0.86), 9, (220, 255, 255))
    sparkle(canvas, int(CW * 0.88), int(CW * 0.78), 7, (255, 240, 200))
    heart(canvas, int(CW * 0.50), int(CW * 0.075), 7, (255, 110, 160))
    heart(canvas, int(CW * 0.50), int(CW * 0.925), 6, (120, 190, 255))

    img = canvas.convert("RGB").resize((W, H), Image.LANCZOS)
    img = img.quantize(colors=15, method=Image.MEDIANCUT).convert("RGB")
    return img


def push(img: Image.Image) -> dict:
    data = img.tobytes()  # RGB, row-major, top to bottom
    payload = json.dumps({
        "Command": "Draw/SendHttpGif",
        "PicNum": 1,
        "PicWidth": W,
        "PicOffset": 0,
        "PicID": 2,
        "PicSpeed": 1000,
        "PicData": base64.b64encode(data).decode(),
    }).encode()
    req = urllib.request.Request(
        f"http://{DEVICE}/post", data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())


def main() -> None:
    img = build_image()
    preview = img.resize((W * SCALE, H * SCALE), Image.NEAREST)
    preview.save("hello_nancy_preview.png")
    img.save("hello_nancy_64.png")
    resp = push(img)
    print("push response:", resp)


if __name__ == "__main__":
    main()
