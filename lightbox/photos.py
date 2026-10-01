"""巡检照片：本地文件存储。

不依赖图像库，照片落盘为 SVG（矢量示意：黑底灯箱 + 点亮灯带 + 元数据戳），
既能在浏览器直接预览，也便于测试断言。返回相对 photo_root 的路径。
"""
from __future__ import annotations

from pathlib import Path

SVG_TEMPLATE = """<svg xmlns="http://www.w3.org/2000/svg" width="480" height="300" viewBox="0 0 480 300">
  <rect width="480" height="300" fill="{bg}"/>
  <rect x="40" y="40" width="400" height="220" rx="10" fill="#16181d" stroke="#3a3f47" stroke-width="2"/>
  {bars}
  <text x="40" y="288" font-family="monospace" font-size="13" fill="#c9ccd1">{code} · {ts_label} · 亮度{brightness_pct:.0f}%（{lit_bars}/{total_bars} 灯带）</text>
</svg>
"""
BAR_COLORS = {False: "#23272e", True: "#ffd36b"}


def render_photo(path: str | Path, *, code: str, ts_label: str, brightness: float,
                 lit_bars: int, total_bars: int) -> str:
    """生成一张示意照片 SVG，返回写入路径。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    width = 360 / max(total_bars, 1)
    bars = []
    for i in range(total_bars):
        x = 60 + i * (width + 10)
        lit = i < lit_bars
        bars.append(
            f'<rect x="{x:.1f}" y="60" width="{width:.1f}" height="180" rx="6" '
            f'fill="{BAR_COLORS[lit]}" />'
        )
    bg = "#f4f1ea" if brightness > 0.5 else ("#20242b" if brightness > 0 else "#0d0f13")
    svg = SVG_TEMPLATE.format(
        bg=bg, bars="\n  ".join(bars), code=code, ts_label=ts_label,
        brightness_pct=brightness * 100, lit_bars=lit_bars, total_bars=total_bars,
    )
    path.write_text(svg, encoding="utf-8")
    return str(path)


class PhotoStore:
    def __init__(self, root: str | Path, url_prefix: str = "/photos/"):
        self.root = Path(root)
        self.url_prefix = url_prefix

    def save(self, *, code: str, inspection_id: int, ts_label: str,
             brightness: float, lit_bars: int, total_bars: int) -> str:
        rel = Path(code) / f"insp-{inspection_id:05d}-{ts_label.replace(':', '').replace(' ', 'T')}.svg"
        render_photo(
            self.root / rel, code=code, ts_label=ts_label,
            brightness=brightness, lit_bars=lit_bars, total_bars=total_bars,
        )
        return str(rel)
