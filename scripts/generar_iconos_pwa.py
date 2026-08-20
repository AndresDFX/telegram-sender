"""Genera los iconos PNG de la PWA (Replica) y el módulo Python que los embebe.

El isotipo de Replica vive como SVG inline en el panel (`_PAGE` de entrypoints/admin.py):
cuadrado redondeado con degradado naranja + tres ondas y cuatro nodos en blanco. Un manifest
de PWA necesita PNG (Chrome/Android exige 192 y 512 para que la app sea instalable), así que
aquí se rasteriza el MISMO glifo con Pillow y se emite:

  docs/brand/replica/pwa/*.png            (referencia visual, versionada)
  src/lambda/entrypoints/pwa_assets.py    (los mismos bytes en base64, que es lo que sirve la Lambda)

El empaquetado de la Lambda solo copia archivos .py (scripts/_build_lambda_pkg.py), de ahí que
los iconos viajen como base64 dentro de un módulo y no como binarios sueltos.

    python scripts/generar_iconos_pwa.py
"""

from __future__ import annotations

import base64
import os
import textwrap

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PNG = os.path.join(ROOT, "docs", "brand", "replica", "pwa")
OUT_PY = os.path.join(ROOT, "src", "lambda", "entrypoints", "pwa_assets.py")

SS = 4  # supersampling: se dibuja a 4x y se reduce con LANCZOS (bordes suaves sin SVG)
AC1 = (253, 83, 30)    # --ac  #FD531E
AC2 = (253, 158, 118)  # #FD9E76
WHITE = (255, 255, 255, 255)


def _gradiente(n: int) -> Image.Image:
    """Degradado diagonal AC1→AC2 (equivale al linearGradient x1=0,y1=0,x2=1,y2=1 del SVG)."""
    g = Image.new("RGB", (64, 64))
    px = g.load()
    for y in range(64):
        for x in range(64):
            t = (x + y) / 126.0
            px[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(AC1, AC2))
    return g.resize((n, n), Image.BICUBIC).convert("RGBA")


def _bezier(p0, c1, c2, p3, pasos: int = 48):
    """Puntos de una cúbica de Bézier (los `c` de los paths del SVG)."""
    out = []
    for i in range(pasos + 1):
        t = i / pasos
        u = 1 - t
        out.append((
            u * u * u * p0[0] + 3 * u * u * t * c1[0] + 3 * u * t * t * c2[0] + t * t * t * p3[0],
            u * u * u * p0[1] + 3 * u * u * t * c1[1] + 3 * u * t * t * c2[1] + t * t * t * p3[1],
        ))
    return out


def _glifo(d: ImageDraw.ImageDraw, k: float, dx: float = 0.0, dy: float = 0.0) -> None:
    """Dibuja las ondas y los nodos del isotipo. `k` = píxeles por unidad del viewBox 48x48."""
    def P(pt):
        return (pt[0] * k + dx, pt[1] * k + dy)

    r = 2.6 * k / 2  # stroke-width 2.6 → radio del trazo
    trazos = [
        _bezier((21, 24), (26, 24), (26.5, 15), (32.5, 15), pasos=260),
        _bezier((21, 24), (24.8, 24), (28.7, 24), (32.5, 24), pasos=260),
        _bezier((21, 24), (26, 24), (26.5, 33), (32.5, 33), pasos=260),
    ]
    # Trazo redondo (stroke-linecap/linejoin="round") estampando círculos a lo largo del path:
    # PIL con line(joint="curve") deja muescas en las diagonales; el estampado no.
    for tr in trazos:
        for p in (P(pt) for pt in tr):
            d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=WHITE)
    for cx, cy, r in [(15, 24, 4.2), (33.5, 15, 3), (34.5, 24, 3), (33.5, 33, 3)]:
        c = P((cx, cy))
        rr = r * k
        d.ellipse([c[0] - rr, c[1] - rr, c[0] + rr, c[1] + rr], fill=WHITE)


def icono(size: int, *, maskable: bool = False, escala: float = 1.0) -> Image.Image:
    """Icono cuadrado. `maskable`: fondo a sangre y glifo reducido (zona segura del 80%)."""
    n = size * SS
    fondo = _gradiente(n)
    lienzo = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    if maskable:
        lienzo = fondo
    else:
        mask = Image.new("L", (n, n), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, n - 1, n - 1], radius=round(12 / 48 * n), fill=255)
        lienzo.paste(fondo, (0, 0), mask)

    capa = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    k = n / 48.0 * escala
    off = (n - 48 * k) / 2.0
    _glifo(ImageDraw.Draw(capa), k, off, off)
    lienzo = Image.alpha_composite(lienzo, capa)
    # Sin paleta/quantize: con 255 colores el degradado diagonal sale con bandas visibles
    # (se probó FASTOCTREE: pesa 4x menos pero escalona el fondo). RGBA de 8 bits por canal.
    return lienzo.resize((size, size), Image.LANCZOS)


ICONOS = {
    "icon-192.png": icono(192),
    "icon-512.png": icono(512),
    # maskable: Android recorta a círculo/squircle; el glifo debe caber en el 80% central.
    "icon-maskable-512.png": icono(512, maskable=True, escala=0.62),
    # iOS redondea las esquinas por su cuenta: fondo a sangre.
    "apple-touch-icon-180.png": icono(180, maskable=True, escala=0.74),
}

os.makedirs(OUT_PNG, exist_ok=True)
for nombre, img in ICONOS.items():
    img.save(os.path.join(OUT_PNG, nombre), optimize=True)
    print(f"  {nombre}: {os.path.getsize(os.path.join(OUT_PNG, nombre))} bytes")

CONST = {
    "icon-192.png": "ICON_192_B64",
    "icon-512.png": "ICON_512_B64",
    "icon-maskable-512.png": "ICON_MASKABLE_512_B64",
    "apple-touch-icon-180.png": "APPLE_TOUCH_180_B64",
}
partes = [
    '"""Iconos PNG de la PWA en base64 — GENERADO por scripts/generar_iconos_pwa.py.\n\n'
    "No editar a mano: vuelve a correr el script (rasteriza el isotipo de Replica con Pillow).\n"
    "Viven como base64 en un .py porque el empaquetado de la Lambda solo copia archivos .py.\n"
    '"""\n',
]
for nombre, const in CONST.items():
    b64 = base64.b64encode(open(os.path.join(OUT_PNG, nombre), "rb").read()).decode()
    cuerpo = "\n".join('    "%s"' % ln for ln in textwrap.wrap(b64, 96))
    partes.append(f"{const} = (\n{cuerpo}\n)\n")
with open(OUT_PY, "w", encoding="utf-8", newline="\n") as fh:
    fh.write("\n".join(partes))
print(f"OK: {OUT_PY} ({os.path.getsize(OUT_PY)} bytes)")
