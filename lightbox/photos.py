"""本地照片：用 zlib+struct 直接写 PNG，不依赖 Pillow。

照片是风格化"巡查快照"：昼夜底、灯箱画面预览色、亮灯灯带、
画面不符的红叉标记。文字信息由界面在照片旁展示。
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path


def _hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _blend(c1, c2, t: float):
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))


def _encode_png(path: Path, width: int, height: int, rows: list[bytearray]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = bytearray()
    for row in rows:
        raw.append(0)  # filter type 0
        raw.extend(row)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", ihdr))
        f.write(chunk(b"IDAT", zlib.compress(bytes(raw), 9)))
        f.write(chunk(b"IEND", b""))


def render_photo(path: str | Path, creative_hex: str, fraction: float,
                 strips_on: int, strips_total: int, within_schedule: bool,
                 creative_match: bool = True, label: str = "") -> str:
    W, H = 300, 200
    night = within_schedule   # 计划亮灯时段对应夜间场景
    bg = (16, 19, 28) if night else (223, 230, 238)
    rows = [bytearray(bg * W) for _ in range(H)]

    def px(x, y, color):
        if 0 <= x < W and 0 <= y < H:
            i = x * 3
            rows[y][i:i + 3] = bytes(color)

    def rect(x0, y0, x1, y1, color):
        color = bytes(color)
        for y in range(y0, y1):
            rows[y][x0 * 3:x1 * 3] = color * (x1 - x0)

    def line(x0, y0, x1, y1, color, thick=1):
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        while True:
            for ox in range(thick):
                for oy in range(thick):
                    px(x0 + ox, y0 + oy, color)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    # 夜间星点（确定性，避免随机）
    if night:
        for i in range(40):
            sx = (i * 73 + 17) % W
            sy = (i * 41 + 11) % 60
            px(sx, sy, (180, 190, 210))

    # 灯箱外框与立柱
    bx0, by0, bx1, by1 = 40, 45, 260, 150
    rect(bx0 - 8, by0 - 8, bx1 + 8, by1 + 8, (40, 42, 48))   # 墨灰框
    rect(144, by1 + 8, 156, H - 6, (60, 62, 70))

    # 画面：预览色按亮灯分数压暗
    base = _hex_rgb(creative_hex)
    panel = _blend((12, 13, 18), base, 0.15 + 0.85 * fraction)
    rect(bx0, by0, bx1, by1, panel)

    # 灯带：面板上下缘
    lit_color = (255, 236, 170)
    off_color = (70, 66, 52)
    n = max(strips_total, 1)
    step = (bx1 - bx0 - 12) / n
    for i in range(n):
        cx = int(bx0 + 6 + step * i + step / 2)
        on = i < strips_on
        rect(cx - 6, by1 - 10, cx + 6, by1 - 4,
             lit_color if on else off_color)
        rect(cx - 6, by0 + 4, cx + 6, by0 + 10,
             lit_color if on else off_color)

    # 画面已撤换/不符：红叉 + 斜条
    if not creative_match:
        line(bx0 + 10, by0 + 10, bx1 - 10, by1 - 10, (210, 60, 60), 4)
        line(bx1 - 10, by0 + 10, bx0 + 10, by1 - 10, (210, 60, 60), 4)

    # 画面文字（简易 5x7 点阵，只画标签首行）
    if label:
        _draw_text(rows, px, bx0 + 12, by0 + 18, label[:22],
                   (250, 250, 250) if fraction > 0.4 else (120, 120, 120))

    # 半亮/全黑角标条
    tag = (230, 180, 60) if 0 < fraction < 1 else (
        (90, 90, 96) if fraction == 0 else (120, 200, 130))
    rect(8, 8, 8 + int((W - 16) * max(fraction, 0.02)), 14, tag)

    _encode_png(Path(path), W, H, rows)
    return str(path)


# 极简 5x7 点阵字体（覆盖 A-Z 0-9 : - 空格 / .）
_FONT = {
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
    ":": ["00000", "00100", "00100", "00000", "00100", "00100", "00000"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
    ".": ["00000", "00000", "00000", "00000", "00000", "01100", "01100"],
    "/": ["00001", "00010", "00010", "00100", "01000", "01000", "10000"],
    " ": ["00000"] * 7,
}
def _draw_text(rows, px, x, y, text, color):
    # 只有数字和符号有字模；字母用 7 段近似的简单块
    cursor = x
    for ch in text.upper():
        glyph = _FONT.get(ch)
        if glyph is None:
            glyph = _FONT[" "]
        for ry, linebits in enumerate(glyph):
            for rx, b in enumerate(linebits):
                if b == "1":
                    px(cursor + rx, y + ry, color)
        cursor += 7
