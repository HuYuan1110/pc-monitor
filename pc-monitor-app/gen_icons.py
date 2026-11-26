# -*- coding: utf-8 -*-
"""生成 App 图标（纯 Python，无依赖）：深色底 + 青蓝渐变监测折线。"""
import struct, zlib, math, os

def png_chunk(tag, data):
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

def make_png(W, H, px_fn):
    px = bytearray()
    for y in range(H):
        px.append(0)
        for x in range(W):
            px += bytes(px_fn(x, y))
    ihdr = struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(px), 9)
    return (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", ihdr)
            + png_chunk(b"IDAT", idat) + png_chunk(b"IEND", b""))

# 折线拐点（归一化坐标），青 -> 蓝渐变
PTS = [(0.16, 0.72), (0.36, 0.60), (0.50, 0.38), (0.70, 0.46), (0.84, 0.26)]
C0 = (52, 211, 153)   # 青绿 #34d399
C1 = (56, 189, 248)   # 亮蓝 #38bdf8
DOT = (34, 211, 238)  # 圆点 #22d3ee

def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))

def seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)

def in_rounded(x, y, W, H, r):
    # 圆角矩形内部判断（含角落）
    if x < r and y < r:
        return (x - r) ** 2 + (y - r) ** 2 <= r * r
    if x >= W - r and y < r:
        return (x - (W - r)) ** 2 + (y - r) ** 2 <= r * r
    if x < r and y >= H - r:
        return (x - r) ** 2 + (y - (H - r)) ** 2 <= r * r
    if x >= W - r and y >= H - r:
        return (x - (W - r)) ** 2 + (y - (H - r)) ** 2 <= r * r
    return 0 <= x < W and 0 <= y < H

def draw_scene(W, H, bg_mode, corner_r):
    """bg_mode: 'square'|'round'|'none'"""
    cx = [p[0] * W for p in PTS]
    cy = [p[1] * H for p in PTS]
    lw = max(3.0, W * 0.055)
    dot_r = max(2.0, W * 0.045)
    dot = (PTS[-1][0] * W, PTS[-1][1] * H)
    bg_r = corner_r if bg_mode != 'none' else 0

    def px_fn(x, y):
        a = 255
        if bg_mode == 'square':
            if not in_rounded(x, y, W, H, bg_r):
                return (0, 0, 0, 0)
        elif bg_mode == 'round':
            if (x - W / 2) ** 2 + (y - H / 2) ** 2 > (W / 2) ** 2:
                return (0, 0, 0, 0)
        # 背景渐变（对角线）
        if bg_mode != 'none':
            t = (x + y) / (W + H)
            r = int(11 + (26 - 11) * t)      # #0b.. -> #1a..
            g = int(18 + (43 - 18) * t)
            b = int(32 + (85 - 32) * t)
        else:
            r = g = b = 0
        # 折线
        best = 1e9
        best_t = 0.0
        for i in range(len(cx) - 1):
            d = seg_dist(x + 0.5, y + 0.5, cx[i], cy[i], cx[i + 1], cy[i + 1])
            if d < best:
                best = d
                best_t = i / (len(cx) - 2) if len(cx) > 2 else 0
        if best <= lw / 2:
            c = lerp(C0, C1, best_t)
            return (c[0], c[1], c[2], a)
        # 末端圆点
        if math.hypot(x + 0.5 - dot[0], y + 0.5 - dot[1]) <= dot_r:
            return (DOT[0], DOT[1], DOT[2], a)
        if bg_mode == 'none':
            return (0, 0, 0, 0)
        return (r, g, b, a)
    return make_png(W, H, px_fn)

def main():
    base = os.path.dirname(os.path.abspath(__file__))
    res = os.path.join(base, "android", "app", "src", "main", "res")
    sizes = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}
    for name, S in sizes.items():
        d = os.path.join(res, "mipmap-" + name)
        # 方形图标（深色圆角底）
        open(os.path.join(d, "ic_launcher.png"), "wb").write(draw_scene(S, S, 'square', S * 0.18))
        # 圆形图标
        open(os.path.join(d, "ic_launcher_round.png"), "wb").write(draw_scene(S, S, 'round', 0))
        # 自适应图标前景（透明底，居中图案）
        open(os.path.join(d, "ic_launcher_foreground.png"), "wb").write(draw_scene(S, S, 'none', 0))
    print("图标已生成到各 mipmap 目录")

if __name__ == "__main__":
    main()
