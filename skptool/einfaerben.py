"""Getoente Texturen (SketchUp "Colorize") nachrechnen.

SketchUp speichert bei einem eingefaerbten Texturmaterial das UNVERAENDERTE Bild, die Materialfarbe
(Zielfarbe) und die Art der Toenung (0 = Farbton verschieben, 1 = einfaerben). Angezeigt wird das
Bild erst nach einer Umrechnung im HLS-Farbraum. Diese Datei rechnet sie nach, fuer Programme, die
das nicht koennen (glTF-Betrachter, die Anzeige in Blender).

Quelle der Formeln: Trimbles eigene Beschreibung des Verfahrens,
github.com/SketchUp/sketchup-colorize-algorithm, Datei cpp/colorize.cpp ("exakt wie SketchUp
selbst, Stand SU2018"). Uebernommen ist die Rechnung, nicht der Code:

  Abweichung (GetColorizeDeltas) von Ausgangsfarbe A zur Zielfarbe Z, beide in HLS
  (Farbton h in Grad 0..360, Helligkeit l und Saettigung s 0..1, grau: h = -1, s = 0):
    A und Z grau:   dh = 0,      ds = 0,          dl = Z.l - A.l
    nur Z grau:     dh = 0,      ds = -1,         dl = Z.l - A.l
    nur A grau:     dh = Z.h,    ds = Z.s,        dl = Z.l - A.l
    sonst:          dh = Z.h,    ds = Z.s - A.s,  dl = Z.l - A.l, beim Verschieben dh = Z.h - A.h
  Je Pixel (Colorize):
    Verschieben: h = h + dh, Einfaerben: h = dh, danach h in 0..360 bringen
    s = s + ds, begrenzt auf 0..1;  l = l + dl, begrenzt auf 0.01..1
    s == 0: Grau mit Helligkeit l, sonst HLS -> RGB; Alpha bleibt.

Ausgangsfarbe A ist die Durchschnittsfarbe des Bildes. SketchUp speichert sie in Dateien ab 2021
als avgColor an der Textur; nachgemessen an allen 15 Texturen von gross_2026 ist sie der
abgerundete Mittelwert der Pixel (sRGB, ohne Gewichtung), genau das rechnet texture_average.
Ob SketchUp das Verfahren seit 2018 geaendert hat, ist nicht bekannt.
"""
from __future__ import annotations

import io

import numpy as np

SHIFT, TINT = 0, 1  # colorize_type wie in OpenSKP und SketchUp
_EPS = 1.0e-3  # EQUAL_TOLERANCE im Original
MAX_PIXELS = 64 * 1024 * 1024  # groesser: nicht umrechnen (Speicher, Dekompressionsbomben)
_CHUNK = 1 << 20


def _rgb_to_hls(rgb):
    """rgb: float-Array (..., 3) in 0..1 -> h (Grad, grau -1), l, s."""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    l = (mx + mn) / 2.0
    d = mx - mn
    grau = np.abs(d) < _EPS
    dd = np.where(grau, 1.0, d)
    s = np.where(l <= 0.5, d / np.where(grau, 1.0, mx + mn), d / np.where(grau, 1.0, 2.0 - mx - mn))
    rc, gc, bc = (mx - r) / dd, (mx - g) / dd, (mx - b) / dd
    h = np.where(np.abs(r - mx) < _EPS, bc - gc,
                 np.where(np.abs(g - mx) < _EPS, 2.0 + rc - bc, 4.0 + gc - rc)) * 60.0
    h = np.where(h < 0, h + 360.0, h)
    return np.where(grau, -1.0, h), l, np.where(grau, 0.0, s)


def _calc_value(n1, n2, hue):
    hue = np.mod(hue, 360.0)
    return np.where(hue < 60, n1 + (n2 - n1) * hue / 60.0,
                    np.where(hue < 180, n2, np.where(hue < 240, n1 + (n2 - n1) * (240 - hue) / 60.0, n1)))


def _to_byte(x):
    return np.clip(np.floor(x * 255.0 + 0.5), 0, 255).astype(np.uint8)  # C++ round: halb weg von 0


def _hls_to_rgb(h, l, s):
    m2 = np.where(l < 0.5, l * (1.0 + s), l + s - l * s)
    m1 = 2.0 * l - m2
    grau = np.abs(s) < _EPS
    out = np.empty(h.shape + (3,), np.uint8)
    for k, off in enumerate((120.0, 0.0, -120.0)):
        out[..., k] = _to_byte(np.where(grau, l, _calc_value(m1, m2, h + off)))
    return out


def colorize_deltas(average_rgb, color_rgb, colorize_type=SHIFT):
    """HLS-Abweichung (dh in Grad, dl, ds) von der Bild-Durchschnittsfarbe zur Materialfarbe."""
    a = np.asarray(average_rgb[:3], np.float64) / 255.0
    z = np.asarray(color_rgb[:3], np.float64) / 255.0
    ah, al, as_ = (float(v) for v in _rgb_to_hls(a))
    zh, zl, zs = (float(v) for v in _rgb_to_hls(z))
    a_grau = len(set(int(c) for c in average_rgb[:3])) == 1  # IsMonochrome: exakt gleiche Bytes
    z_grau = len(set(int(c) for c in color_rgb[:3])) == 1
    dl = zl - al
    if a_grau and z_grau:
        return 0.0, dl, 0.0
    if z_grau:
        return 0.0, dl, -1.0
    if a_grau:
        return zh, dl, zs
    return (zh - ah if colorize_type == SHIFT else zh), dl, zs - as_


def colorize_pixels(rgba, deltas, colorize_type=SHIFT):
    """uint8-Array (N, 3 oder 4) umrechnen, Rueckgabe gleiche Form. Alpha bleibt unveraendert."""
    dh, dl, ds = deltas
    out = rgba.copy()
    for i in range(0, len(rgba), _CHUNK):
        part = rgba[i:i + _CHUNK, :3].astype(np.float64) / 255.0
        h, l, s = _rgb_to_hls(part)
        h = h + dh if colorize_type == SHIFT else np.full_like(h, dh)
        h = np.mod(h, 360.0)
        s = np.clip(s + ds, 0.0, 1.0)
        l = np.clip(l + dl, 0.01, 1.0)
        out[i:i + _CHUNK, :3] = _hls_to_rgb(h, l, s)
    return out


def _open(data):
    from PIL import Image
    im = Image.open(io.BytesIO(data))
    w, h = im.size
    if w <= 0 or h <= 0 or w * h > MAX_PIXELS:
        raise ValueError("Bild zu gross oder leer")
    return im


def texture_average(data):
    """Durchschnittsfarbe wie SketchUps avgColor (abgerundeter Mittelwert) oder None."""
    try:
        with _open(data) as im:
            px = np.asarray(im.convert("RGB"), np.uint8).reshape(-1, 3)
    except Exception:
        return None
    return tuple(int(v) for v in np.floor(px.mean(axis=0, dtype=np.float64)))


def colorized_png(data, color_rgb, colorize_type=SHIFT):
    """Bild so getoent, wie SketchUp es anzeigt, als PNG-Bytes, oder None (nicht lesbar, zu gross)."""
    from PIL import Image
    try:
        with _open(data) as im:
            im = im.convert("RGBA")
            w, h = im.size
            px = np.asarray(im, np.uint8).reshape(-1, 4)
    except Exception:
        return None
    avg = tuple(int(v) for v in np.floor(px[:, :3].mean(axis=0, dtype=np.float64)))
    out = colorize_pixels(px, colorize_deltas(avg, color_rgb, colorize_type), colorize_type)
    buf = io.BytesIO()
    img = Image.fromarray(out.reshape(h, w, 4), "RGBA")
    if (out[:, 3] == 255).all():
        img = img.convert("RGB")
    img.save(buf, "PNG")
    return buf.getvalue()
