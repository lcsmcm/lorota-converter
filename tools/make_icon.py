"""Gera o ícone do Lorota Converter (icon.ico) — quadrado arredondado em degradê com equalizador."""
import sys
from PIL import Image, ImageDraw

S = 256
out = sys.argv[1] if len(sys.argv) > 1 else "icon.ico"

# degradê diagonal rosa -> laranja
grad = Image.new("RGBA", (S, S))
c1, c2 = (255, 61, 139), (255, 165, 59)
px = grad.load()
for y in range(S):
    for x in range(S):
        t = (x + y) / (2 * S - 2)
        px[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(c1, c2)) + (255,)

mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle((8, 8, S - 8, S - 8), radius=58, fill=255)
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
img.paste(grad, (0, 0), mask)

# barras de equalizador
d = ImageDraw.Draw(img)
heights = [0.35, 0.7, 0.5, 0.9, 0.6]
bw, gap = 26, 14
total = len(heights) * bw + (len(heights) - 1) * gap
x0, base = (S - total) // 2, 196
for i, h in enumerate(heights):
    x = x0 + i * (bw + gap)
    d.rounded_rectangle((x, base - h * 136, x + bw, base), radius=13, fill=(26, 11, 31, 255))

img.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("ícone salvo em", out)
