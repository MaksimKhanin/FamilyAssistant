"""Сгенерировать иконки приложения.

    python -m scripts.make_icons

Рисует ту же «С» в скруглённом квадрате акцентного цвета, что стоит в углу
сайдбара. Иконки лежат в репозитории готовыми — скрипт нужен, только если менять
цвет или форму. Написан на zlib без графических библиотек: тянуть Pillow ради
четырёх картинок, которые рисуются один раз, незачем.
"""
import math
import struct
import zlib
from pathlib import Path

OUT_DIR = Path("app/static/icons")

# Акцент тёплого оформления: иконка на экране «Домой» одна на устройство, а
# оформление своё у каждого — значит, рисуем в дефолтном, а не в чьём-то.
ACCENT = (0xC0, 0x56, 0x3C)
INK = (0xFF, 0xF4, 0xF0)

#: Полная иконка и «maskable» — у второй буква мельче, чтобы Android мог обрезать
#: её в круг, не задев (безопасная зона — центральные 80%).
SIZES = [
    ("icon-192.png", 192, 0.62, 0.16),
    ("icon-512.png", 512, 0.62, 0.16),
    ("icon-maskable-512.png", 512, 0.46, 0.00),   # фон во всю плитку, буква мельче
    ("apple-touch-icon.png", 180, 0.62, 0.00),    # iOS сама скругляет углы
    ("favicon-32.png", 32, 0.70, 0.20),
]


def _rounded(x: float, y: float, size: float, radius: float) -> bool:
    """Точка внутри скруглённого квадрата?"""
    if radius <= 0:
        return True
    dx = max(radius - x, 0, x - (size - radius))
    dy = max(radius - y, 0, y - (size - radius))
    return dx * dx + dy * dy <= radius * radius


def _letter_c(dx: float, dy: float, outer: float, inner: float) -> bool:
    """Кольцо с вырезанным правым сектором — узнаваемая «С»."""
    distance = math.hypot(dx, dy)
    if not inner <= distance <= outer:
        return False
    angle = math.degrees(math.atan2(dy, dx))
    return not -42 <= angle <= 42


def render(size: int, letter_scale: float, corner_ratio: float) -> list:
    radius = size * corner_ratio
    centre = size / 2
    outer = size * letter_scale / 2
    inner = outer * 0.62
    thickness = max(1.0, size * 0.055)
    inner = max(inner, outer - thickness * 1.6)

    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            px, py = x + 0.5, y + 0.5
            if not _rounded(px, py, size, radius):
                row += bytes(INK)                     # «прозрачный» угол — цветом фона страницы
            elif _letter_c(px - centre, py - centre, outer, inner):
                row += bytes(INK)
            else:
                row += bytes(ACCENT)
        rows.append(bytes(row))
    return rows


def write_png(path: Path, size: int, rows: list):
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + row for row in rows)     # 0 = фильтр «None» для строки
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    path.write_bytes(png)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, size, letter_scale, corner_ratio in SIZES:
        write_png(OUT_DIR / name, size, render(size, letter_scale, corner_ratio))
        print(f"  {OUT_DIR / name}  ({size}×{size})")


if __name__ == "__main__":
    main()
