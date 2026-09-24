"""SketchUp-Operationen auf Basis von OpenSKP (reines Python, kein SketchUp, kein Trimble-SDK)."""
from __future__ import annotations

import collections
import contextlib
import dataclasses
import json
import math
import os
import re
from pathlib import Path

import numpy as np

from openskp import SkpFile, create
from openskp import edit as _edit
from openskp.create import ComponentDefinitionBuilder, SkpBuilder, SkpWriteError
from openskp.export import dxf, ifc, json_export, obj, ply, stl

# Texturausrichtung: SketchUp leitet die Texturachsen einer Flaeche allein aus ihrer Normale ab
# (openskp._face_groups.face_uv_basis, vom Leser benutzt). OpenSKP 1.2.0 nahm beim Schreiben
# stattdessen die erste Kante, skptool ersetzte das durch die Basis des Lesers. Seit 1.3.0 rechnet
# der Writer selbst mit face_uv_basis, der Ersatz entfaellt; der Import bricht ab, falls sich das
# wieder aendert (tests/test_openskp_vertrag.py weist zusaetzlich nach, dass OpenSKPs eigene
# Matrix gedrehte, schraege und nach unten zeigende Flaechen richtig legt).
# Geblieben ist die perspektivische Texturmatrix ab 4 Punktpaaren, die OpenSKP nicht kennt.
import importlib as _importlib

from openskp._face_groups import face_uv_basis as _sketchup_face_uv_basis

OPENSKP_GETESTET = "1.3.0"
# Achtung: "from openskp import create" liefert die Funktion create(), nicht das Modul.
_openskp_create_module = _importlib.import_module("openskp.create")
# Ersetzt wird nur, was es gibt: ein fehlender Name wuerde sonst still neu angelegt und nie
# aufgerufen, verzerrte Texturen fielen dann ohne Fehlermeldung weg.
# tests/test_openskp_vertrag.py prueft zusaetzlich, dass die Ersetzung wirklich greift.
for _name in ("_uv_matrix_for_face", "_solve_uv_matrix"):
    if not callable(getattr(_openskp_create_module, _name, None)):
        raise ImportError(f"OpenSKP-Version passt nicht zu skptool: openskp.create.{_name} fehlt. "
                          f"Getestet mit {OPENSKP_GETESTET}.")
if getattr(_openskp_create_module, "face_uv_basis", None) is not _sketchup_face_uv_basis:
    raise ImportError("OpenSKP-Version passt nicht zu skptool: der Writer (openskp.create) rechnet die "
                      "Texturachsen nicht mehr mit face_uv_basis des Lesers. "
                      f"Getestet mit {OPENSKP_GETESTET}.")


def _uv_matrix_for_face(points, pairs, normal):
    """Texturmatrix wie SketchUp sie speichert: (u, v, 1) @ M = (x, y, 1) in der Flaechenbasis.

    3 Punktpaare: affin, genau wie OpenSKP. Ab 4 Paaren: perspektivisch (SketchUps "fixierte
    Pins", verzerrte Texturen). Dafuer wird die Homographie H mit (x, y, 1) @ H ~ (u, v, 1) aus
    allen Paaren geschaetzt (DLT) und M = H^-1 gespeichert, genau die Umkehrung dessen, was der
    Leser und SketchUp beim Anzeigen rechnen. points bleibt fuer die Aufrufstelle in OpenSKP.
    Die Paare kommen seit OpenSKP 1.3.0 schon mit der Kachelgroesse des Materials skaliert an
    (create._scale_pins in add_face), hier ist nichts weiter zu tun."""
    if len(pairs) == 3:
        return _openskp_create_module._solve_uv_matrix(pairs, _sketchup_face_uv_basis(normal))
    xr, yr = _sketchup_face_uv_basis(normal)
    rows = []
    for p, (u, v) in pairs:
        x = p[0] * xr[0] + p[1] * xr[1] + p[2] * xr[2]
        y = p[0] * yr[0] + p[1] * yr[1] + p[2] * yr[2]
        # Unbekannte h (zeilenweise 3x3): u * (r . h_col2) - (r . h_col0) = 0, analog fuer v
        r = [x, y, 1.0]
        rows.append([-r[0], 0, u * r[0], -r[1], 0, u * r[1], -r[2], 0, u * r[2]])
        rows.append([0, -r[0], v * r[0], 0, -r[1], v * r[1], 0, -r[2], v * r[2]])
    a = np.asarray(rows, np.float64)
    scale = np.abs(a).max() or 1.0
    h = np.linalg.svd(a / scale)[2][-1].reshape(3, 3)
    try:
        m = np.linalg.inv(h)
    except np.linalg.LinAlgError:
        raise SkpWriteError("verzerrte Textur nicht darstellbar") from None
    if abs(m[2, 2]) > 1e-12:
        m = m / m[2, 2]
    return tuple(float(v) for v in m.reshape(-1))


_openskp_create_module._uv_matrix_for_face = _uv_matrix_for_face

# Flaechen in Dreiecke zerlegen (Szene, GLB fuer Blender, diff): OpenSKP trianguliert Vierecke stur
# ueber die Ecken 0-2 (auch 1.3.0), groessere Flaechen in 1.2.0 per Delaunay ueber die Eckpunkte (bei
# konkaven Flaechen fehlten Teile: eine Wand der Gondel verlor 10 % ihrer Flaeche), seit 1.3.0 per
# earcut. Beides legt bei Zwischenpunkten auf einer Kante Dreiecke ohne Flaeche an, die der SKP-Writer
# verwirft, und konkave Vierecke falsch. Gemessen mit 1.3.0 ueber alle Flaechen: gondel_2020 45 Dreiecke
# ohne Flaeche (hier 0), gross_2026 447 ohne Flaeche, 55 mit falschem Umlauf, 434 Flaechen falsch
# abgedeckt (hier 0, 4, 380 mit 1/50 des Flaechenfehlers). Deshalb bleibt der Ersatz: eingeschraenkte
# Delaunay-Triangulierung, die die Kanten einhaelt, in der Projektion und mit dem Umlaufsinn von
# OpenSKP. Einfache Faelle (Dreiecke, klar konvexe Vierecke) bleiben bei OpenSKP.
_openskp_core_module = _importlib.import_module("openskp._core")
if not callable(getattr(_openskp_core_module, "triangulate_face_3d", None)):
    raise ImportError("OpenSKP-Version passt nicht zu skptool: openskp._core.triangulate_face_3d fehlt. "
                      f"Getestet mit {OPENSKP_GETESTET}.")
_triangulate_face_openskp = _openskp_core_module.triangulate_face_3d


def _triangulate_face_3d(vertices_3d, loops, normal):
    if len(loops) == 1 and len(loops[0]) == 3:
        return _triangulate_face_openskp(vertices_3d, loops, normal)
    try:
        tris = _constrained_face_triangles(vertices_3d, loops, normal)
    except Exception:  # noqa: BLE001 - dann wie bisher
        tris = None
    return tris or _triangulate_face_openskp(vertices_3d, loops, normal)


def _constrained_face_triangles(vertices_3d, loops, normal):
    """Dreiecke als Punkt-ID-Listen oder None (dann triangulate_face_3d von OpenSKP)."""
    import shapely
    from shapely.geometry import Polygon

    n = np.asarray(normal, np.float64)
    length = np.linalg.norm(n)
    n = n / length if length > 1e-6 else np.array([0.0, 0.0, 1.0])
    u = np.cross(n, [1.0, 0.0, 0.0] if abs(n[0]) < 0.9 else [0.0, 1.0, 0.0])  # wie OpenSKP
    u /= np.linalg.norm(u)
    v = np.cross(n, u)
    ids = [i for lp in loops for i in lp]
    if any(i not in vertices_3d for i in ids):
        return None
    xy = {i: (float(np.dot(vertices_3d[i], u)), float(np.dot(vertices_3d[i], v))) for i in ids}
    rings = [[xy[i] for i in lp] for lp in loops]

    def signed_area(ring):
        return sum(ax * by - bx * ay for (ax, ay), (bx, by) in zip(ring, ring[1:] + ring[:1])) / 2

    sign = 1.0 if signed_area(rings[0]) >= 0 else -1.0  # Umlaufsinn der Aussenschleife beibehalten
    if len(loops) == 1 and len(rings[0]) == 4:
        # klar konvexes Viereck (kein Punkt fast auf einer Geraden): dieselbe Zerlegung wie OpenSKP
        pts = rings[0]
        whole = abs(signed_area(pts))
        if all(sign * signed_area([pts[k - 1], pts[k], pts[(k + 1) % 4]]) > 1e-6 * whole for k in range(4)):
            return None
    poly = Polygon(rings[0], rings[1:])
    if not poly.is_valid:  # z. B. Schleifen, die sich in einem Punkt beruehren
        parts = shapely.get_parts(shapely.get_parts(shapely.make_valid(poly)))  # auch aus Sammlungen
        poly = shapely.MultiPolygon([p for p in parts if p.geom_type == "Polygon"])
    if poly.area <= 0:
        return None
    by_xy = {}
    for i in ids:
        by_xy.setdefault(xy[i], i)
    tris = []
    for tri in shapely.constrained_delaunay_triangles(poly).geoms:
        corners = list(tri.exterior.coords)[:3]
        tid = [by_xy.get(c) for c in corners]
        if None in tid or len(set(tid)) < 3:
            return None
        if sign * signed_area(corners) < 0:
            tid = [tid[0], tid[2], tid[1]]
        tris.append(tid)
    return tris


_openskp_core_module.triangulate_face_3d = _triangulate_face_3d

# Materialnamen je Flaeche: OpenSKP fasst die Flaechen einer Definition nach dem Aussehen zusammen
# (Farbe, doppelseitig, Textur, Deckkraft), nicht nach dem Material. Zwei Materialien gleicher
# Farbe landen so in EINEM Primitiv mit EINEM glTF-Material, der Name war nur noch ueber die Farbe
# zu raten (Gondel: "mat1" auf 114 Flaechen kam als "*5" zurueck, beide weiss). Hier bekommt die
# aufgeloeste Farbe den Materialnamen mit: ein Tupel wie bisher (r, g, b), gleich nur bei gleicher
# Farbe UND gleichem Material. Damit trennen Gruppierung und glTF-Materialliste von OpenSKP die
# Materialien von selbst, ohne dass an Instanzen oder Geometrie etwas anders wird.
# Zusaetzlich traegt die Farbe der Rueckseite einer zweiseitig verschieden bemalten Flaeche das
# Kennzeichen "Rueckseite": Vorder- und Rueckseiten liegen dann in getrennten Primitiven, die
# Vorderseiten zuerst. Blender behaelt von zwei deckungsgleichen Seiten die zuerst stehende
# (bridge._backface_duplicates), so bleibt die Vorderseite vorne. Vorher entschied die zufaellige
# Reihenfolge der Gruppen, und mit den neuen Namensgruppen drehten sich in gross_2026 rund 2000
# Flaechen um. Gleiche glTF-Materialien fasst build_instanced_scene danach wieder zusammen.
# build_instanced_scene (unten) merkt sich je Netz die Namen seiner Primitive in Reihenfolge.
_openskp_face_groups_module = _importlib.import_module("openskp._face_groups")
_openskp_instanced_module = _importlib.import_module("openskp.instanced_scene")
for _mod, _name in ((_openskp_face_groups_module, "resolve_color"),
                    (_openskp_face_groups_module, "_add_face_side"),
                    (_openskp_instanced_module, "build_local_face_groups"),
                    (_openskp_instanced_module, "build_instanced_scene")):
    if not callable(getattr(_mod, _name, None)):
        raise ImportError(f"OpenSKP-Version passt nicht zu skptool: {_mod.__name__}.{_name} fehlt. "
                          f"Getestet mit {OPENSKP_GETESTET}.")


class _NamedColor(tuple):
    """(r, g, b) mit Materialname und Seite. Vergleich und Hash schliessen beides ein."""

    def __new__(cls, rgb, material, back=False):
        self = super().__new__(cls, rgb)
        self.material = material
        self.back = back
        self._hash = hash((tuple(rgb), material, back))
        return self

    def __hash__(self):
        return self._hash

    def __eq__(self, other):
        if self is other:
            return True
        return (tuple.__eq__(self, other) is True and getattr(other, "material", None) == self.material
                and getattr(other, "back", False) == self.back)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __reduce__(self):
        return (_NamedColor, (tuple(self), self.material, self.back))


_resolve_color_openskp = _openskp_face_groups_module.resolve_color
_add_face_side_openskp = _openskp_face_groups_module._add_face_side
_named_colors: dict = {}


def _named_color(rgb, material, back=False):
    key = (tuple(rgb), material, back)
    color = _named_colors.get(key)
    if color is None:
        if len(_named_colors) > 100_000:  # viele Dateien in einem Prozess: nicht endlos wachsen
            _named_colors.clear()
        color = _named_colors[key] = _NamedColor(key[0], material, back)
    return color


def _resolve_color_named(mat):
    rgb = _resolve_color_openskp(mat)
    return None if rgb is None else _named_color(rgb, mat.get("name"))


def _add_face_side_sided(face_groups, builder, triangles, fn, color, double_sided, reverse, *rest):
    if reverse:  # Rueckseite: eigene Gruppe (auch unbemalt, dann ohne Namen)
        color = _named_color(color, getattr(color, "material", None), True)
    return _add_face_side_openskp(face_groups, builder, triangles, fn, color, double_sided, reverse, *rest)


_openskp_face_groups_module.resolve_color = _resolve_color_named
_openskp_face_groups_module._add_face_side = _add_face_side_sided

# Deckkraft: OpenSKP multipliziert die Deckkraft eines Materials mit dem Alpha seiner Farbe. Bei
# Texturen ist dieses Alpha aber nur der Platzhalter der Durchschnittsfarbe, den der 2017-Writer (und
# set_texture_average_color) mit 254 schreibt, weil 255 dort "eingefaerbt" heisst. Jede so
# geschriebene Textur wurde in der GLB halbdurchsichtig (alphaMode BLEND, 0.996), Blender sortierte
# sie wie Glas, und ueber Blender kam sie mit Deckkraft 0.996 zurueck; durchsichtiges Glas mit
# Textur wurde aus 0.5 zu 0.498. Bei Texturen zaehlt deshalb nur die eigentliche Deckkraft, das
# Alpha 254 des Platzhalters wird wie 255 behandelt. Uebrig bleibende Werte ab 254/255 gelten als
# deckend (eine Farbe mit Alpha 254 ist nicht sichtbar durchsichtig, BLEND schadet nur).
if not callable(getattr(_openskp_face_groups_module, "resolve_transparency", None)):
    raise ImportError("OpenSKP-Version passt nicht zu skptool: openskp._face_groups.resolve_transparency "
                      f"fehlt. Getestet mit {OPENSKP_GETESTET}.")
_resolve_transparency_openskp = _openskp_face_groups_module.resolve_transparency


def _resolve_transparency_deckend(mat):
    if mat and mat.get("texture") and (mat.get("color") or {}).get("a") == 254:
        mat = {**mat, "color": {**mat["color"], "a": 255}}
    t = _resolve_transparency_openskp(mat)
    return 1.0 if t >= 254 / 255 else t


_openskp_face_groups_module.resolve_transparency = _resolve_transparency_deckend

import threading as _threading  # noqa: E402

_build_groups_openskp = _openskp_instanced_module.build_local_face_groups
_recording = _threading.local()


def _build_groups_recording(builder, ctx):
    groups = _build_groups_openskp(builder, ctx)
    if any(getattr(key[0], "back", False) for key in groups):  # Vorderseiten zuerst (stabil)
        groups = dict(sorted(groups.items(), key=lambda kv: bool(getattr(kv[0][0], "back", False))))
    names = getattr(_recording, "names", None)
    if names is not None:  # gleiche Auswahl und Reihenfolge wie die Primitive in build_instanced_scene
        names.append([getattr(key[0], "material", None) for key, g in groups.items() if g["local_faces"]])
    return groups


_openskp_instanced_module.build_local_face_groups = _build_groups_recording


def _names_per_gltf_material(isc, recorded):
    """SketchUp-Materialname je glTF-Material (None = unbemalt, Farbe geerbt) oder None, wenn die
    Aufzeichnung nicht zur Szene passt (dann raet gltf_writer wie frueher ueber die Farbe)."""
    calls = [names for names in recorded if names]  # leere Aufrufe ergeben kein Netz
    if len(calls) != len(isc.mesh_resources):
        return None
    out: list = [None] * len(isc.gltf_materials)
    seen = [False] * len(isc.gltf_materials)
    for res, names in zip(isc.mesh_resources, calls):
        if len(names) != len(res.primitives):
            return None
        for prim, name in zip(res.primitives, names):
            i = int(prim.material_index)
            if not 0 <= i < len(out) or (seen[i] and out[i] != name):
                return None
            out[i], seen[i] = name, True
    return out


def _merge_equal_materials(isc, names):
    """Gleiche glTF-Materialien mit gleichem SketchUp-Namen (Vorder- und Rueckseitengruppe) zu
    einem zusammenfassen, die Primitive zeigen dann auf dieses."""
    index: dict = {}
    remap, merged, merged_names = [], [], []
    for i, gm in enumerate(isc.gltf_materials):
        key = (json.dumps(gm, sort_keys=True), names[i])
        if key not in index:
            index[key] = len(merged)
            merged.append(gm)
            merged_names.append(names[i])
        remap.append(index[key])
    for res in isc.mesh_resources:
        for prim in res.primitives:
            prim.material_index = remap[int(prim.material_index)]
    isc.gltf_materials = merged
    return merged_names


def build_instanced_scene(skp):
    """openskp.instanced_scene.build_instanced_scene aus dem schon geparsten Ergebnis, dazu
    isc.skp_material_names: SketchUp-Materialname je glTF-Material (None = unbemalte Flaeche).

    name_override_keys=(): OpenSKP 1.3.0 wuerde Instanzen sonst nach Eintraegen "name", "label"
    oder "code" beliebiger Attribut-Woerterbuecher benennen; skptool bleibt bei den Namen aus
    SketchUp selbst (Instanz, sonst Definition). Die Knotennamen bilden zusammen mit der Position
    den Schluessel fuer _instance_info (Ebene, Bemalung) und muessen dazu passen."""
    if skp._parsed is None:
        model_of(skp)
    _recording.names = recorded = []
    try:
        isc = _openskp_instanced_module.build_instanced_scene(skp._parsed, name_override_keys=())
    finally:
        _recording.names = None
    names = _names_per_gltf_material(isc, recorded)
    isc.skp_material_names = _merge_equal_materials(isc, names) if names is not None else None
    return isc


INCH = 0.0254  # SketchUp speichert intern in Zoll
NATIVE_FORMATS = {".glb", ".obj", ".stl", ".ply", ".dxf", ".ifc", ".json", ".3mf"}
BLENDER_OUT_FORMATS = {".blend", ".fbx", ".usd", ".usda", ".usdc", ".usdz", ".abc", ".gltf", ".png"}
BLENDER_IN_FORMATS = {".blend", ".glb", ".gltf", ".fbx", ".obj", ".stl", ".ply",
                      ".usd", ".usda", ".usdc", ".usdz", ".abc"}


def header_version(path: str | Path) -> str:
    """Liest die Versionskennung direkt aus dem Dateikopf, auch wenn das Parsen scheitert.

    Nur die ersten 200 Byte werden gelesen, nie die ganze Datei: eine praeparierte Riesendatei
    (mehrere GB) darf beim blossen "info" den Speicher nicht sprengen."""
    with open(path, "rb") as fh:
        head = fh.read(200)
    if "SketchUp".encode("utf-16-le") not in head:
        raise ValueError(f"{path} ist keine SketchUp-Datei")
    text = head.decode("utf-16-le", errors="ignore")
    m = re.search(r"\{([\d.]+)\}", text)
    return m.group(1) if m else "?"


def format_generation(version: str) -> str:
    major = int(version.split(".")[0]) if version[:1].isdigit() else 0
    if major >= 21:
        return f"SketchUp 20{major} (neues Format ab 2021)"
    if major >= 13:
        return f"SketchUp 20{major:02d} (klassisches Format)"
    return f"SketchUp {major} (klassisches Format)"


MAX_UNZIPPED = int(os.environ.get("SKPTOOL_MAX_UNZIPPED_GB", "8")) * 2**30
MAX_ZIP_ENTRIES = 100_000
MAX_ZIP_RATIO = 100  # echte Dateien liegen bei etwa 4
MAX_PLACEMENTS = int(os.environ.get("SKPTOOL_MAX_PLACEMENTS", "5000000"))


class UnsafeFileError(ValueError):
    """Datei ueberschreitet Sicherheitsgrenzen (z. B. ZIP-Bombe, explodierende Instanzen)."""


def check_container(path: str | Path) -> None:
    """Vor dem Einlesen: Dateien ab SketchUp 2021 sind ZIP-Container. OpenSKP liest sie komplett
    in den Speicher und prueft nur grob auf ZIP-Bomben, deshalb hier ein eigenes Budget."""
    import zipfile

    try:
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
    except zipfile.BadZipFile:
        return  # Format vor 2021, kein ZIP
    except NotImplementedError as exc:
        # zipfile wirft das bei nicht unterstuetzten ZIP-Versionen oder Kompressionsarten (z. B. wenn
        # ein Feld im lokalen Kopf verdreht ist). Eine echte .skp braucht so etwas nie: klar ablehnen,
        # statt den Fehler als Traceback nach aussen dringen zu lassen.
        raise UnsafeFileError(f"{path}: Container laesst sich nicht lesen ({exc})") from None
    if len(infos) > MAX_ZIP_ENTRIES:
        raise UnsafeFileError(f"{path}: {len(infos)} Eintraege im Container (Grenze {MAX_ZIP_ENTRIES})")
    total = sum(i.file_size for i in infos)
    if total > MAX_UNZIPPED:
        raise UnsafeFileError(f"{path}: entpackt {total / 2**30:.1f} GB (Grenze {MAX_UNZIPPED / 2**30:.0f} GB, "
                              "anhebbar mit SKPTOOL_MAX_UNZIPPED_GB)")
    for i in infos:
        if i.file_size > 2**20 and i.file_size / max(i.compress_size, 1) > MAX_ZIP_RATIO:
            raise UnsafeFileError(f"{path}: Eintrag {i.filename!r} ist verdaechtig stark komprimiert "
                                  f"({i.file_size / max(i.compress_size, 1):.0f}:1)")


def open_skp(path: str | Path) -> SkpFile:
    """Datei einmal einlesen. Das Modell bleibt am Objekt, siehe model_of()."""
    check_container(path)
    skp = SkpFile.open(str(path))
    skp._skptool_model = skp.parse()
    return skp


def model_of(skp: SkpFile):
    """Geparstes Modell ohne erneutes Einlesen (SkpFile.parse() liest bei jedem Aufruf neu)."""
    model = getattr(skp, "_skptool_model", None)
    if model is None:
        model = skp._skptool_model = skp.parse()
    return model


def build_scene(skp: SkpFile, curves: bool = False):
    """Szene aus dem bereits geparsten Ergebnis bauen.

    SkpFile.build_scene() liest die Datei absichtlich ein zweites Mal komplett ein, was bei
    grossen Dateien die Laufzeit fast verdoppelt. parse() speichert das Rohergebnis bereits,
    und der Szenenaufbau veraendert es nicht; glb.export nutzt denselben Weg.

    curves: lose Kanten als Linienzuege (Scene.curve_sets, seit OpenSKP 1.3.0). Nur der
    IFC-Export liest sie, alle anderen Wege sparen sich die Arbeit.
    name_override_keys=(): OpenSKP 1.3.0 wuerde Instanzen sonst nach Eintraegen "name", "label"
    oder "code" beliebiger Attribut-Woerterbuecher benennen; skptool bleibt bei den Namen aus
    SketchUp selbst (Instanz, sonst Definition), wie in build_instanced_scene().
    """
    from openskp import scene as _scene

    if skp._parsed is None:
        model_of(skp)
    return _scene.build_scene(skp._parsed, name_override_keys=(), include_curve_sets=curves)


# ---------------------------------------------------------------- info

def info(path: str | Path, with_bounds: bool = False, skp: SkpFile | None = None) -> dict:
    """Kennzahlen einer .skp. skp: schon geoeffnete Datei (spart ein zweites Einlesen)."""
    version = header_version(path)
    if skp is None:
        skp = open_skp(path)
    m = model_of(skp)
    inst_count: collections.Counter = collections.Counter()
    for d in [m.root, *m.definitions.values()]:
        for inst in d.instances:
            inst_count[inst.ref_idx] += 1
    comps = [{"name": d.name or f"(Gruppe #{def_id})", "faces": len(d.faces),
              "instances": inst_count.get(def_id, 0)}
             for def_id, d in m.definitions.items() if not d.is_image]
    comps.sort(key=lambda c: (-c["instances"], c["name"]))
    result = {
        "file": str(path),
        "size_bytes": Path(path).stat().st_size,
        "version": version,
        "format": format_generation(version),
        "faces_total": len(m.root.faces) + sum(len(d.faces) for d in m.definitions.values()),
        "layers": [{"name": l.name, "hidden": bool(l.hidden),
                    "color": [l.color_r, l.color_g, l.color_b]} for l in m.layers],
        "materials": [{"name": mt.name, "rgba": list(mt.color) if mt.color else None,
                       "textured": bool(mt.texture)} for mt in m.materials],
        "components": comps,
        "scenes": [getattr(p, "name", str(p)) for p in (m.pages or [])],
    }
    if with_bounds:
        scene = build_scene(skp)
        lo = [math.inf] * 3
        hi = [-math.inf] * 3
        tris = 0
        for prim in scene.glb_primitives:
            pos = prim.positions
            for k in range(3):
                axis = pos[k::3]
                if len(axis):
                    lo[k] = min(lo[k], min(axis))
                    hi[k] = max(hi[k], max(axis))
            tris += len(prim.indices) // 3
        if tris:  # Szene ist Y-up in Metern
            result["size_m"] = {"width": round(hi[0] - lo[0], 3),
                                "depth": round(hi[2] - lo[2], 3),
                                "height": round(hi[1] - lo[1], 3)}
        result["triangles"] = tris
    return result


# ---------------------------------------------------------------- Export (SKP -> offene Formate)

def export_native(skp: SkpFile, out: Path, textures: bool = True) -> Path:
    ext = out.suffix.lower()
    out.parent.mkdir(parents=True, exist_ok=True)
    if ext == ".glb":  # instanzerhaltend, eindeutige Geometrie nur einmal
        from skptool.gltf_writer import write_instanced_glb

        m = model_of(skp)
        write_instanced_glb(m, build_instanced_scene(skp), _instance_info(m), out, textures=textures,
                            bake_colorize=True)
        return out
    if ext == ".3mf":  # 3D-Druck: Millimeter, Z oben, eindeutige Netze einmal, Hinweise auf stderr
        from skptool.export_3mf import write_3mf

        write_3mf(model_of(skp), build_instanced_scene(skp), out, max_components=MAX_PLACEMENTS)
        return out
    # IFC: seit OpenSKP 1.3.0 in Millimetern und Z oben (vorher Zoll-Werte unter der Einheit mm
    # und Y oben), lose Kanten kommen als IfcAnnotation mit
    scene = build_scene(skp, curves=ext == ".ifc")
    if ext == ".obj":
        obj.export(scene, out)
    elif ext == ".stl":
        stl.export(scene, out, binary=True)
    elif ext == ".ply":
        ply.export(scene, out, binary=True)
    elif ext == ".dxf":
        dxf.export(scene, out)
    elif ext == ".ifc":
        ifc.export(scene, out)
    elif ext == ".json":
        json_export.export(model_of(skp), out, scene=scene)
    else:
        raise ValueError(f"Nicht unterstuetztes Zielformat: {ext}")
    return out


def _instance_info(model) -> dict:
    """Ebene und geerbte Bemalung je platzierter Instanz, Schluessel wie OpenSKPs mesh_index.

    OpenSKP uebernimmt Instanz-Ebenen und Gruppenbemalung beim Szenenaufbau nur aus dem
    Format ab 2021. Fuer aeltere Dateien wird beides hier aus dem Modellbaum ergaenzt.
    Ebene: die aeusserste Gruppe mit eigener Ebene. Bemalung: die innerste bemalte Gruppe.
    """
    from openskp import _core

    info = {}

    def key(path, mm):
        return (path, *(round(mm[i] * 25.4, 2) + 0.0 for i in (9, 10, 11)))

    def walk(defn, matrix, path, layer, paint, depth=0):
        if depth > 64:
            return
        for inst in defn.instances:
            ref = model.definitions.get(inst.ref_idx)
            if ref is None:
                continue
            mm = _core.multiply_matrices(matrix, inst.matrix)
            p = f"{path} / {inst.name or ref.name or 'Gruppe'}"  # wie gltf_writer
            # innerste eigene Ebene gewinnt (z. B. "Staender" in einer Gruppe "Rahmen")
            lay = inst.layer if inst.layer not in (None, "", "Layer0") else layer
            mat = model.materials_by_id.get(inst.material_id)
            pt = mat.name if mat else paint
            info[key(p, mm)] = (lay, pt)
            if len(info) > MAX_PLACEMENTS:
                raise UnsafeFileError(f"mehr als {MAX_PLACEMENTS} Platzierungen (verschachtelte Komponenten "
                                      "vervielfachen sich), anhebbar mit SKPTOOL_MAX_PLACEMENTS")
            walk(ref, mm, p, lay, pt, depth + 1)

    walk(model.root, [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1.0], "ROOT", "Layer0", None)
    return info


def export_for_blender(skp: SkpFile, glb_path: Path, meta_path: Path, textures: bool = True) -> dict:
    """Instanzerhaltende GLB (Meter, Y-up) plus kleine Metadaten fuer Blender.

    Jede eindeutige Geometrie steht nur einmal in der Datei, Blender macht daraus verknuepfte
    Kopien. Ebene, geerbte Bemalung und Komponentenname haengen als extras an jedem Knoten.
    """
    from skptool.gltf_writer import write_instanced_glb

    m = model_of(skp)
    isc = build_instanced_scene(skp)
    stats = write_instanced_glb(m, isc, _instance_info(m), glb_path, textures=textures)
    hard = stats.pop("hard_edges", {})
    meta = {
        "hard_edges": hard,
        "soft_edges": stats.pop("soft_edges", {}),
        "layers": [{"name": l.name, "hidden": bool(l.hidden)} for l in m.layers],
        "materials": [{"name": mt.name, "rgb": list(mt.color[:3]) if mt.color else [255, 255, 255],
                       "alpha": mt.transparency if mt.transparency is not None else 1.0,
                       "textured": bool(mt.texture)} for mt in m.materials if mt.id is not None],
    }
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    return stats


# ---------------------------------------------------------------- SKP -> SKP im 2017-Format

def save_atomic(builder, out: Path) -> None:
    """Erst komplett im Speicher bauen, dann per Umbenennen ersetzen.

    SkpBuilder.save() oeffnet die Zieldatei vor dem Aufbau; scheitert der, bleibt eine leere
    Datei zurueck (eine vorhandene 773-KB-Datei wurde so zu 0 Byte). Hier bleibt das Ziel bei
    jedem Fehler unveraendert."""
    try:
        data = builder.to_bytes()
    except SkpWriteError as exc:
        if "no geometry" in str(exc):
            raise ValueError("Keine Flaechen zum Schreiben: SketchUp-Dateien brauchen mindestens eine "
                             "Flaeche (reine Linien, Kurven oder leere Modelle gehen nicht)") from None
        raise
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.skptool-tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, out)
    finally:
        if tmp.exists():
            tmp.unlink()


def _newell(points):
    nx = ny = nz = 0.0
    for i, a in enumerate(points):
        b = points[(i + 1) % len(points)]
        nx += (a[1] - b[1]) * (a[2] + b[2])
        ny += (a[2] - b[2]) * (a[0] + b[0])
        nz += (a[0] - b[0]) * (a[1] + b[1])
    return nx, ny, nz


def triangulate_polygon(points, holes=()):
    """Nicht-planares Polygon (optional mit Loechern) in echte Dreiecke zerlegen.

    Projiziert auf die Best-Fit-Ebene und nutzt die eingeschraenkte Delaunay-Triangulierung
    von Shapely, damit auch konkave Flaechen und Loecher korrekt bleiben. Die Dreiecke
    verwenden die Original-3D-Punkte. Ersetzt auto_triangulate des OpenSKP-Writers: das faechert
    (auch in 1.3.0) nur vom ersten Punkt aus auf, was konkave Flaechen falsch zerlegt, und kann
    weder Loecher noch Texturpunkte. (Mit 1.2.0 war seine Ausgabe zudem teils nicht wieder lesbar.)
    """
    import shapely
    from shapely.geometry import Polygon

    nx, ny, nz = _newell(points)
    length = math.sqrt(nx * nx + ny * ny + nz * nz)
    if length < 1e-12:
        return []
    n = (nx / length, ny / length, nz / length)
    ref = (1.0, 0.0, 0.0) if abs(n[0]) < 0.9 else (0.0, 1.0, 0.0)
    u = (n[1] * ref[2] - n[2] * ref[1], n[2] * ref[0] - n[0] * ref[2], n[0] * ref[1] - n[1] * ref[0])
    ul = math.sqrt(sum(c * c for c in u))
    u = tuple(c / ul for c in u)
    v = (n[1] * u[2] - n[2] * u[1], n[2] * u[0] - n[0] * u[2], n[0] * u[1] - n[1] * u[0])
    lookup = {}

    def proj(ring):
        out = []
        for p in ring:
            q = (sum(p[k] * u[k] for k in range(3)), sum(p[k] * v[k] for k in range(3)))
            lookup[(round(q[0], 9), round(q[1], 9))] = tuple(p)
            out.append(q)
        return out

    poly = Polygon(proj(points), [proj(h) for h in holes if len(h) >= 3])
    if not poly.is_valid or poly.area <= 0:
        return []
    tris = []
    for tri in shapely.constrained_delaunay_triangles(poly).geoms:
        corners = []
        for x, y in list(tri.exterior.coords)[:3]:
            key = (round(x, 9), round(y, 9))
            p3 = lookup.get(key)
            if p3 is None:  # Rundungsdifferenz: naechsten Originalpunkt nehmen
                p3 = min(lookup.items(), key=lambda kv: (kv[0][0] - x) ** 2 + (kv[0][1] - y) ** 2)[1]
            corners.append(p3)
        if len(set(corners)) == 3:
            # Umlaufrichtung an der Ursprungsflaeche ausrichten, sonst tauschen Vorder- und
            # Rueckseite (und damit deren Materialien) die Seiten.
            a, b, c = corners
            tn = _newell([a, b, c])
            if tn[0] * n[0] + tn[1] * n[1] + tn[2] * n[2] < 0:
                corners = [a, c, b]
            tris.append(corners)
    return tris


def _uv_from_pins(pins, points):
    """UV fuer beliebige Punkte einer Flaeche aus drei Texturpunkten (affine Abbildung in der
    Flaechenebene). Fuer Dreiecke, die beim Zerlegen einer unebenen Flaeche entstehen."""
    (p0, t0), (p1, t1), (p2, t2) = pins
    e1 = np.subtract(p1, p0)
    e2 = np.subtract(p2, p0)
    a = np.array([[e1 @ e1, e1 @ e2], [e1 @ e2, e2 @ e2]])
    if abs(np.linalg.det(a)) < 1e-18:
        return None
    ainv = np.linalg.inv(a)
    out = {}
    for p in points:
        d = np.subtract(p, p0)
        s_, t_ = ainv @ np.array([d @ e1, d @ e2])
        out[p] = (t0[0] + s_ * (t1[0] - t0[0]) + t_ * (t2[0] - t0[0]),
                  t0[1] + s_ * (t1[1] - t0[1]) + t_ * (t2[1] - t0[1]))
    return out


def _strip_uv(kwargs):
    return {k: v for k, v in kwargs.items() if k not in ("front_uv", "back_uv")}


def add_face_safe(orig, target, points, stats, *args, uv_lookup=None, **kwargs):
    """add_face mit eigener Triangulierung fuer nicht-planare Flaechen.

    Texturausrichtung bleibt dabei erhalten: jedes Dreieck bekommt eigene Texturpunkte, aus
    uv_lookup (echte UV je Eckpunkt) oder aus den Texturpunkten der ganzen Flaeche errechnet.
    Texturpunkte mit NaN oder unendlich (etwa aus einer praeparierten Kachelgroesse der Quelle)
    fallen weg: der OpenSKP-Writer wuerde daraus still eine NaN-Texturmatrix schreiben.
    """
    for key in ("front_uv", "back_uv"):
        pins = kwargs.get(key)
        if pins is not None and not all(math.isfinite(float(c)) for p, t in pins for c in (*p, *t)):
            kwargs.pop(key)
            stats["uv_dropped"] = stats.get("uv_dropped", 0) + 1
    try:
        return orig(target, points, *args, **kwargs)
    except SkpWriteError as exc:
        err = str(exc)
    if "coplanar" not in err:
        if "front_uv" in kwargs or "back_uv" in kwargs:  # z. B. unbrauchbare Texturpunkte
            try:
                orig(target, points, *args, **_strip_uv(kwargs))
                stats["uv_dropped"] = stats.get("uv_dropped", 0) + 1
                return None
            except SkpWriteError:
                pass
        stats["skipped"] += 1
        return None
    tris = triangulate_polygon(list(points), kwargs.pop("holes", ()) or ())
    kwargs.pop("auto_triangulate", None)
    if not tris:
        stats["skipped"] += 1
        return None
    stats["triangulated"] += 1
    front, back = kwargs.get("front_uv"), kwargs.get("back_uv")
    corner_uv = {"front_uv": None, "back_uv": None}
    if front is not None:
        corner_uv["front_uv"] = uv_lookup or _uv_from_pins(front, [p for t in tris for p in t])
    if back is not None:
        corner_uv["back_uv"] = (uv_lookup if back == front else None) or             _uv_from_pins(back, [p for t in tris for p in t])
    for tri in tris:
        kw = dict(kwargs)
        for key, table in corner_uv.items():
            if key in kw:
                if table is not None and all(p in table for p in tri):
                    kw[key] = [(p, tuple(table[p])) for p in tri]
                else:
                    kw.pop(key)
        try:
            orig(target, tri, *args, **kw)
        except SkpWriteError:
            try:
                orig(target, tri, *args, **_strip_uv(kw))
                stats["uv_dropped"] = stats.get("uv_dropped", 0) + 1
            except SkpWriteError:
                stats["skipped"] += 1
    return None


@contextlib.contextmanager
def tolerant_faces(stats: dict):
    """Nicht-planare Flaechen selbst triangulieren, unbrauchbare ueberspringen und zaehlen."""
    originals = {cls: cls.add_face for cls in (SkpBuilder, ComponentDefinitionBuilder)}

    def make(orig):
        def add_face(self, points, *args, **kwargs):
            return add_face_safe(orig, self, points, stats, *args, **kwargs)
        return add_face

    for cls, orig in originals.items():
        cls.add_face = make(orig)
    try:
        yield
    finally:
        for cls, orig in originals.items():
            cls.add_face = orig


_AVG_PLACEHOLDER = bytes([255, 255, 255, 254, 0, 255, 255, 255, 254])


def set_texture_average_color(builder, rgb) -> None:
    """Durchschnittsfarbe des zuletzt geschriebenen Texturmaterials eintragen.

    OpenSKP schreibt dort immer Weiss als Platzhalter. SketchUp nutzt die Farbe fuer den
    Materialbrowser und Stile ohne Texturen, OpenSKP beim Einlesen, um Vorder- und Rueckseite zu
    unterscheiden: bei zwei weissen Texturmaterialien fiel die Rueckseitentextur dort weg.
    Alpha bleibt 254, 255 hiesse "eingefaerbt"."""
    buf = builder._material_writer.buf
    i = buf.rfind(_AVG_PLACEHOLDER)
    if i >= 0 and rgb:
        r, g, b = (max(0, min(255, int(c))) for c in rgb[:3])
        buf[i:i + len(_AVG_PLACEHOLDER)] = bytes([r, g, b, 254, 0, r, g, b, 254])


_COLORIZE_TAIL = b"\xff\xfe\xff\x00" + bytes([1, 0, 0, 0, 0, 0, 0, 0])  # leerer Name, Block (1, 0)


def set_texture_colorize(builder, rgb) -> bool:
    """Das zuletzt geschriebene Texturmaterial als eingefaerbt (SketchUp "Colorize") markieren.

    Im Format bis 2020 speichert SketchUp bei einer getoenten Textur das Originalbild, als
    "Durchschnittsfarbe" die Zielfarbe mit Alpha 255 und im Block danach eine 1 im zweiten u32
    (so liest es openskp.legacy._texture_block: eines der beiden Zeichen genuegt). OpenSKPs Writer
    kennt kein Colorize und schreibt Weiss mit Alpha 254 und (1, 0); hier werden beide Zeichen gesetzt.
    Die Art der Toenung (verschieben oder einfaerben) hat in diesem Datensatz kein bekanntes Feld,
    OpenSKP liest solche Materialien immer als "einfaerben". Rueckgabe False, wenn der Datensatz
    nicht wie erwartet aussieht (dann bleibt er unveraendert)."""
    buf = builder._material_writer.buf
    i = buf.rfind(_AVG_PLACEHOLDER)
    tail = i + len(_AVG_PLACEHOLDER)
    if i < 0 or not rgb or bytes(buf[tail:tail + len(_COLORIZE_TAIL)]) != _COLORIZE_TAIL:
        return False
    r, g, b = (max(0, min(255, int(c))) for c in rgb[:3])
    buf[i:tail] = bytes([r, g, b, 255, 0, r, g, b, 255])
    flag = tail + 4 + 4  # nach dem leeren Namen und dem ersten u32
    buf[flag:flag + 4] = (1).to_bytes(4, "little")
    return True


AVERAGE_MAX_PIXELS = 64 * 1024 * 1024  # groesser: Durchschnittsfarbe auslassen statt GBs zu dekodieren


def image_average_rgb(path):
    """Durchschnittsfarbe eines Bildes oder None. Die Groesse wird vorher nur aus dem Kopf gelesen,
    damit ein praepariertes Riesenbild (Dekompressionsbombe) nicht dekodiert wird."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            w, h = im.size
            if w <= 0 or h <= 0 or w * h > AVERAGE_MAX_PIXELS:
                return None
            im.draft("RGB", (max(1, w // 8), max(1, h // 8)))  # JPEG: verkleinert dekodieren
            return im.convert("RGB").resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))
    except Exception:
        return None


def _tile_size(value) -> float:
    """Kachelgroesse aus der Quelldatei (Zoll) oder 1.0. Die Datei gilt als feindlich: NaN,
    unendlich, 0, negativ oder absurd gross (ueber 1000 km) wuerde jede Texturmatrix verderben."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 1.0
    return v if math.isfinite(v) and 1e-9 < v <= MAX_INCHES else 1.0


def _replay_materials(builder, model, warnings):
    """Wie openskp.edit._replay_materials, aber mit Deckkraft (Glas bleibt durchsichtig)."""
    import os
    import tempfile

    slots = {}
    for mat in model.materials:
        opacity = mat.transparency if mat.transparency is not None and mat.transparency < 0.999 else None
        if mat.texture is not None and mat.texture.data:
            suffix = Path(mat.texture.filename or "texture").suffix or ".png"
            fd, tmp_path = tempfile.mkstemp(suffix=suffix)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(mat.texture.data)
                # Echte Kachelgroesse wie in der Quelle (Materialbrowser, in SketchUp neu bemalte
                # Flaechen). Seit OpenSKP 1.3.0 skaliert add_face die Texturpunkte (in Kacheln)
                # selbst mit dieser Groesse, die Lage jeder Flaeche bleibt gleich. Mit 1.2.0 musste
                # hier 1 Zoll je Kachel stehen, sonst waere doppelt geteilt worden.
                slot = builder.add_texture_material(mat.name, tmp_path,
                                                    applied_width=_tile_size(mat.texture.width),
                                                    applied_height=_tile_size(mat.texture.height),
                                                    opacity=opacity)
                if mat.colorized:  # Originalbild plus Zielfarbe, wie SketchUp es speichert
                    if not set_texture_colorize(builder, mat.color):
                        warnings.append(f"Material {mat.name!r}: Farbtoenung der Textur geht verloren")
                        set_texture_average_color(builder, image_average_rgb(tmp_path))
                else:
                    set_texture_average_color(builder, mat.color or image_average_rgb(tmp_path))
            finally:
                os.unlink(tmp_path)
        else:
            slot = builder.add_material(mat.name, mat.color, opacity=opacity)
        slots[id(mat)] = slot
    return slots


def _edge_points(defn):
    """(Kante, p, q) je Kante mit zwei verschiedenen Endpunkten, Punkte wie in edit._replay_face."""
    for e in defn.edges.values():
        v1, v2 = defn.vertices.get(e.v1_id), defn.vertices.get(e.v2_id)
        if v1 is None or v2 is None:
            continue
        p = (float(v1.x), float(v1.y), float(v1.z))
        q = (float(v2.x), float(v2.y), float(v2.z))
        if p != q:
            yield e, p, q


def _predeclare_edges(target, defn) -> int:
    """Alle Kanten einer Definition mit ihren eigenen Flags vor den Flaechen anlegen.

    edit._replay_face setzt weich/glatt/verborgen je FLAECHE ("irgendeine Randkante ist weich"),
    und der Writer vergibt die Flags nur an Kanten, die ein add_face-Aufruf NEU anlegt: eine
    Flaeche mit einer weichen Kante machte so alle ihre neuen Kanten weich, wer zuerst kam,
    bestimmte die Kante. Vorab angelegt behaelt jede Kante ihre Flags, add_face findet sie im
    Kantenregister und aendert sie nicht mehr. Lose Kanten (ohne Flaeche), die edit gar nicht
    uebertraegt, kommen dabei mit. Rueckgabe: Zahl der geschriebenen Kanten."""
    if isinstance(target, SkpBuilder):
        target._ensure_geometry_writer()
        writer = target._geometry_writer
    else:
        writer = target._skp._definition_writer
    count = 0
    for e, p, q in _edge_points(defn):
        _, _, new = writer._write_edge_chain([p, q], target._vertex_slots, target._edge_registry, False,
                                             e.hidden, e.soft, e.smooth)
        target._new_entity_count += new
        count += new
    if count and isinstance(target, SkpBuilder):
        target._face_count += 1  # wie add_polyline: "mindestens ein Objekt im Modell"
    return count


class _SoftDiagonals:
    """add_face mit weichen, glatten Kanten fuer alles, was erst dort entsteht.

    Nach _predeclare_edges sind das nur noch die Diagonalen, die beim Zerlegen einer unebenen
    Flaeche entstehen. SketchUp glaettet solche Diagonalen selbst, wie der Blender-Weg."""

    def __init__(self, target):
        self._target = target

    def add_face(self, points, **kwargs):
        kwargs.update(soft_edges=True, smooth_edges=True, hidden_edges=False)
        return self._target.add_face(points, **kwargs)


# ---------------------------------------------------------------- Anmerkungen und Attribute

# Laengster Text, den der 2017-Writer schreiben kann: _ArchiveWriter._write_str kennt nur die
# kurze MFC-Laenge (ein Byte, 0xFF ist das Escape) und bricht sonst MITTEN im Schreiben ab.
# Alles Laengere wird vorher aussortiert, sonst waere der Puffer halb beschrieben.
_STR_MAX = 254


@dataclasses.dataclass
class Anmerkungen:
    """Texte und Bemassungen eines Modells, getrennt nach schreibbar und nicht schreibbar.

    texte/masse: {Definitions-ID oder "ROOT": [...]} in den Koordinaten dieser Definition, so
    wie sie der 2017-Writer schreiben kann. Die Zaehler nennen, was davon nicht geht."""
    texte: dict
    masse: dict
    texte_gesamt: int = 0
    texte_ohne_anker: int = 0
    texte_zu_lang: int = 0
    masse_gesamt: int = 0
    masse_ohne_endpunkte: int = 0
    masse_zu_lang: int = 0


def _punkt(p):
    try:
        q = tuple(float(c) for c in p)
    except (TypeError, ValueError):
        return None
    return q if len(q) == 3 and all(math.isfinite(c) for c in q) else None


def _str_ok(s) -> bool:
    return isinstance(s, str) and len(s.encode("utf-16-le")) // 2 <= _STR_MAX


def anmerkungen(model, parsed=None) -> Anmerkungen:
    """Welche Texte und Bemassungen der 2017-Writer uebernehmen kann (fuer rewrite_legacy und report).

    Texte: OpenSKP liefert Ankerpunkt und Beschriftungspunkt nur fuer frei gesetzte Texte (in
    Koordinaten ihrer Definition). An Geometrie verankerte Texte haben keinen Punkt.
    Bemassungen: Das Modell behaelt je Definition nur Text und Sichtbarkeit, die Endpunkte stehen
    bei Dateien vor 2021 nur im Rohergebnis des Parsers (parsed, SkpFile._parsed). Dateien ab 2021
    fuehren alle Bemassungen nur auf Modellebene in Weltkoordinaten (model.dimensions)."""
    a = Anmerkungen(texte={}, masse={})
    raw_defs = (parsed or {}).get("defs_dict") or {}
    defs = {**model.definitions, "ROOT": model.root}
    for key, defn in defs.items():
        for t in getattr(defn, "texts", None) or []:
            a.texte_gesamt += 1
            p = _punkt(getattr(t, "point", None))
            if p is None:
                a.texte_ohne_anker += 1
                continue
            text = getattr(t, "text", "") or ""
            if not _str_ok(text):
                a.texte_zu_lang += 1
                continue
            label = _punkt(getattr(t, "label_point", None)) or p
            a.texte.setdefault(key, []).append((text, p, label, bool(getattr(t, "hidden", False))))
        dims = getattr(defn, "dimensions", None) or []
        raw = getattr((raw_defs.get(key) or {}).get("builder"), "dimensions", None) or []
        for i, d in enumerate(dims):
            r = raw[i] if i < len(raw) and isinstance(raw[i], dict) else {}
            _mass_eintragen(a, key, r.get("a") or getattr(d, "a", None), r.get("b") or getattr(d, "b", None),
                            r.get("offset", getattr(d, "offset", 0.0)), getattr(d, "text", "") or "",
                            bool(getattr(d, "hidden", False)))
    if not any(getattr(d, "dimensions", None) for d in defs.values()):
        # Ab 2021: nur die Modellliste. Vor 2021 steht sie zusaetzlich in root.dimensions (doppelt).
        for d in getattr(model, "dimensions", None) or []:
            _mass_eintragen(a, "ROOT", getattr(d, "a", None), getattr(d, "b", None),
                            getattr(d, "offset", 0.0), getattr(d, "text", "") or "",
                            bool(getattr(d, "hidden", False)))
    return a


def _mass_eintragen(a: Anmerkungen, key, p, q, offset, text, hidden) -> None:
    a.masse_gesamt += 1
    p, q = _punkt(p), _punkt(q)
    try:
        offset = float(offset or 0.0)
    except (TypeError, ValueError):
        offset = 0.0
    if p is None or q is None or p == q or not math.isfinite(offset):
        a.masse_ohne_endpunkte += 1
    elif not _str_ok(text):
        a.masse_zu_lang += 1
    else:
        a.masse.setdefault(key, []).append((p, q, offset, text, hidden))


def _writer_of(target):
    """Der Archiv-Writer, in den target (Modell oder Definition) seine Objekte schreibt."""
    if isinstance(target, SkpBuilder):
        target._ensure_geometry_writer()
        return target._geometry_writer
    return target._skp._definition_writer


def _counted(target) -> None:
    target._new_entity_count += 1
    if isinstance(target, SkpBuilder):
        target._face_count += 1  # wie add_text: "mindestens ein Objekt im Modell"


def _font(writer, builder) -> None:
    """Eine Schrift je Datei, beim ersten Text oder Mass inline, danach als Rueckverweis.

    Definitionen stehen in der Datei vor dem Modell, eine dort angelegte Schrift ist fuer alles
    Spaetere (auch das Modell) ein gueltiger Rueckverweis. Die Nummern sind dateiweit."""
    if builder._dim_font_slot is None:
        builder._dim_font_slot = writer._new_of_known_class("CSkFont", schema=1)
        writer.buf += _openskp_create_module._DIM_FONT_PAYLOAD
    else:
        writer._backref(builder._dim_font_slot)


def write_text(target, builder, text: str, point, label, hidden: bool = False) -> None:
    """Freier Text mit Fuehrungslinie in ein Modell ODER eine Definition schreiben.

    Byte fuer Byte wie SkpBuilder.add_text (tests/test_openskp_vertrag.py vergleicht das), nur
    mit echter Sichtbarkeit statt fester Zeichenbasis und auch in Definitionen. OpenSKP kann das
    nur im Modell (add_text gibt es nur am SkpBuilder)."""
    if not _str_ok(text):
        raise SkpWriteError(f"Text zu lang (hoechstens {_STR_MAX} Zeichen)")
    c = _openskp_create_module
    w = _writer_of(target)
    w._new_of_known_class("CText", schema=9)
    w._preamble()
    w._drawbase(hidden=hidden)
    _font(w, builder)
    w.buf += c._f64(0.0) + c._f64(0.0)  # Bildschirmanteil, bei freien Texten unbenutzt
    w.buf += c._u32(1) + c._u32(4)  # freier Anker
    w.buf += b"".join(c._f64(v) for v in point)
    w.buf += bytes(12)
    w.buf += b"".join(c._f64(v) for v in label)
    w.buf += bytes(16)
    w.buf += c._f64(1.0)
    w.buf += c._u32(2)  # Fuehrungslinie: Stecknadel
    w.buf += c._TEXT_DELIM
    w._write_str(text)
    w.buf += bytes(5)
    _counted(target)


def write_dimension(target, builder, p, q, offset: float = 10.0, text: str = "", hidden: bool = False) -> None:
    """Freie lineare Bemassung in ein Modell ODER eine Definition schreiben.

    Byte fuer Byte wie SkpBuilder.add_dimension (Vertragstest), zusaetzlich mit eigenem Text
    ("" = gemessener Wert) und Sichtbarkeit. Die Ebene der Masslinie kennt der Writer nicht."""
    p, q = _punkt(p), _punkt(q)
    if p is None or q is None or p == q:
        raise SkpWriteError("Bemassung braucht zwei verschiedene Endpunkte")
    if not _str_ok(text):
        raise SkpWriteError(f"Text zu lang (hoechstens {_STR_MAX} Zeichen)")
    c = _openskp_create_module
    w = _writer_of(target)
    w._new_of_known_class("CDimensionLinear", schema=6)
    w._preamble()
    w._drawbase(hidden=hidden)
    w._write_str(text)
    _font(w, builder)
    w.buf += bytes(5) + c._u32(1) + c._u32(4)  # Anschluss 1: freier Punkt, kein Objektverweis
    w.buf += b"".join(c._f64(v) for v in p) + bytes(2)
    w.buf += bytes(10) + c._u32(1) + c._u32(4)  # Anschluss 2
    w.buf += b"".join(c._f64(v) for v in q) + bytes(2)
    w.buf += bytes(2)
    for val in (0.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0):  # Vorgaben des SDK fuer freie Masse
        w.buf += c._f64(val)
    w.buf += c._u32(0)
    w.buf += c._f64(float(offset)) + c._f64(0.0) + c._u32(1)
    _counted(target)


_ATTR_TIEFE = 32  # verschachtelte Felder: tiefer ist in keiner echten Datei zu erwarten


def _attr_value(v, tiefe=0):
    """Leserwert -> Writerwert. Rueckgabe (Wert, ok). OpenSKP liest Punkt und Vektor beide als
    3-Tupel (Listen sind Felder), Zeitstempel als int, Wahrheitswerte vor 2021 als int."""
    c = _openskp_create_module
    if tiefe > _ATTR_TIEFE:  # feindliche Datei: Rekursion des Writers nicht bis ans Limit treiben
        return None, False
    if v is None or isinstance(v, (bool, float)):
        return v, True
    if isinstance(v, str):
        return v, _str_ok(v)
    if isinstance(v, int):
        if -2**31 <= v < 2**31:
            return v, True
        if 0 <= v < 2**32:
            return c.Timestamp(v), True
        return float(v), True
    if isinstance(v, tuple) and len(v) == 3 and all(isinstance(x, (int, float)) and not isinstance(x, bool)
                                                   for x in v):
        return c.Point3d(*(float(x) for x in v)), True
    if isinstance(v, (list, tuple)):
        out = []
        for x in v:
            y, ok = _attr_value(x, tiefe + 1)
            if not ok:
                return None, False
            out.append(y)
        return out, True
    return None, False


def attribute_dicts(inst, stats=None) -> list:
    """Alle Attribut-Woerterbuecher einer Platzierung so, wie der Writer sie annimmt.

    Werte, die er nicht schreiben kann (Text ueber 254 Zeichen, unbekannter Typ), fallen einzeln
    weg und werden in stats["attribute_lost"] gezaehlt, der Rest des Woerterbuchs bleibt."""
    source = getattr(inst, "attribute_dictionaries", None) or {}
    if not source and getattr(inst, "properties", None):  # aeltere Leser: nur dynamic_attributes als Text
        source = {"dynamic_attributes": dict(inst.properties)}
    out = []
    lost = 0
    for name, entries in source.items():
        if not name or not _str_ok(name) or not isinstance(entries, dict):
            lost += len(entries) if isinstance(entries, dict) else 1
            continue
        clean = {}
        for key, value in entries.items():
            v, ok = _attr_value(value)
            if ok and key and _str_ok(key):
                clean[key] = v
            else:
                lost += 1
        out.append((name, clean))
    if stats is not None:
        stats["attribute_lost"] = stats.get("attribute_lost", 0) + lost
        stats["attribute_dicts"] = stats.get("attribute_dicts", 0) + len(out)
    return out


def _replay_instance(target, inst, def_builders, material_slots, layer_slots, model, warnings, context, stats):
    """Wie edit._replay_instance, aber mit allen Attribut-Woerterbuechern in ihren echten Typen.

    edit uebertraegt nur "dynamic_attributes", und das als Text (Instance.properties)."""
    def_builder = def_builders.get(inst.ref_idx)
    if def_builder is None:
        warnings.append(f"{context}: instance {inst.name!r} references unavailable definition - skipped")
        return
    matrix3x3 = tuple(inst.matrix[0:9]) if len(inst.matrix) >= 9 else None
    translation = tuple(inst.matrix[9:12]) if len(inst.matrix) >= 12 else (0.0, 0.0, 0.0)
    material = _edit._material_slot(inst.material_id, model, material_slots)
    layer = layer_slots.get(inst.layer) if inst.layer else None
    dicts = attribute_dicts(inst, stats)
    try:
        # name=inst.name wie in edit: ein leerer Name ist ein echter Name
        target.add_instance(def_builder, name=inst.name, translation=translation, matrix3x3=matrix3x3,
                            material=material, layer=layer, hidden=inst.hidden, attribute_dicts=dicts)
    except SkpWriteError as exc:
        warnings.append(f"{context}: instance {inst.name!r} skipped ({exc})")


def _replay_notes(target, builder, notes, warnings, context, stats) -> None:
    texte, masse = notes
    for text, point, label, hidden in texte:
        try:
            write_text(target, builder, text, point, label, hidden)
            stats["texts"] = stats.get("texts", 0) + 1
        except SkpWriteError as exc:  # vorher geprueft, sollte nicht vorkommen
            warnings.append(f"{context}: Text {text[:40]!r} nicht geschrieben ({exc})")
            stats["texts_failed"] = stats.get("texts_failed", 0) + 1
    for p, q, offset, text, hidden in masse:
        try:
            write_dimension(target, builder, p, q, offset, text, hidden)
            stats["dimensions"] = stats.get("dimensions", 0) + 1
        except SkpWriteError as exc:
            warnings.append(f"{context}: Bemassung nicht geschrieben ({exc})")
            stats["dimensions_failed"] = stats.get("dimensions_failed", 0) + 1


def _replay_body(target, defn, model, material_slots, layer_slots, warnings, context, def_builders, stats,
                 builder=None, notes=((), ())):
    """Wie edit._replay_body, aber mit den Kantenflags je Kante statt je Flaeche, allen
    Attributen der Platzierungen und den Texten und Bemassungen dieser Definition."""
    stats["edges"] = stats.get("edges", 0) + _predeclare_edges(target, defn)
    edges = _edit._edge_map(defn)
    faces = _SoftDiagonals(target)
    for face in defn.faces.values():
        _edit._replay_face(faces, face, defn, edges, model, material_slots, warnings, context)
    for inst in defn.instances:
        _replay_instance(target, inst, def_builders, material_slots, layer_slots, model, warnings, context, stats)
    if notes[0] or notes[1]:
        _replay_notes(target, builder, notes, warnings, context, stats)


def _mehrzahl(n: int, eins: str, viele: str) -> str:
    return f"{n} {eins if n == 1 else viele}"


def dynamic_instances(model) -> list[str]:
    """Namen aller Platzierungen mit "dynamic_attributes" (SketchUps dynamische Komponenten)."""
    return [inst.name or getattr(model.definitions.get(inst.ref_idx), "name", "") or "?"
            for d in [model.root, *model.definitions.values()] for inst in d.instances
            if "dynamic_attributes" in (getattr(inst, "attribute_dictionaries", None) or {})]


def _namen(items, n=5) -> str:
    items = list(items)
    text = ", ".join(f'"{x}"' for x in items[:n])
    return text + (f" und {len(items) - n} weitere" if len(items) > n else "")


def verlust_zeilen(model, a: Anmerkungen, stats: dict | None = None) -> list[str]:
    """Je Art, die beim Umschreiben ins 2017-Format verloren geht, eine deutsche Zeile mit Anzahl.

    Ohne stats (skptool report) die Vorhersage aus dem gelesenen Modell, mit stats die Zahlen
    des echten Laufs. Gleiche Saetze an beiden Stellen, damit Bericht und Umschreiben
    zusammenpassen."""
    if stats is None:  # Vorhersage: dieselbe Pruefung der Attribute wie beim Schreiben
        stats = {}
        for d in [model.root, *model.definitions.values()]:
            for i in d.instances:
                attribute_dicts(i, stats)
    out = []
    n = len(getattr(model, "pages", None) or [])
    if n:
        out.append(f"Szenen: {_mehrzahl(n, 'Szene geht', 'Szenen gehen')} verloren "
                   "(der 2017-Writer kann keine Szenen schreiben)")
    defs = [model.root, *model.definitions.values()]
    n = sum(len(getattr(d, "section_planes", None) or []) for d in defs)
    if n:
        out.append(f"Schnittebenen: {_mehrzahl(n, 'Schnittebene geht', 'Schnittebenen gehen')} verloren "
                   "(der 2017-Writer kann keine Schnittebenen schreiben)")
    lost = a.texte_ohne_anker + a.texte_zu_lang + stats.get("texts_failed", 0)
    if lost:
        why = [w for k, w in ((a.texte_ohne_anker, f"{a.texte_ohne_anker} ohne freien Ankerpunkt"),
                              (a.texte_zu_lang, f"{a.texte_zu_lang} laenger als {_STR_MAX} Zeichen"),
                              (stats.get("texts_failed", 0), f"{stats.get('texts_failed', 0)} vom Writer abgelehnt"))
               if k]
        out.append(f"Texte: {lost} von {a.texte_gesamt} {'geht' if lost == 1 else 'gehen'} verloren "
                   f"({', '.join(why)})")
    lost = a.masse_ohne_endpunkte + a.masse_zu_lang + stats.get("dimensions_failed", 0)
    if lost:
        why = [w for k, w in ((a.masse_ohne_endpunkte, f"{a.masse_ohne_endpunkte} ohne lesbare Endpunkte"),
                              (a.masse_zu_lang, f"{a.masse_zu_lang} mit Text ueber {_STR_MAX} Zeichen"),
                              (stats.get("dimensions_failed", 0),
                               f"{stats.get('dimensions_failed', 0)} vom Writer abgelehnt")) if k]
        out.append(f"Bemassungen: {lost} von {a.masse_gesamt} {'geht' if lost == 1 else 'gehen'} verloren "
                   f"({', '.join(why)})")
    insts = [i for d in defs for i in d.instances]
    dyn = dynamic_instances(model)
    if dyn:
        out.append(f"Dynamische Komponenten: {_mehrzahl(len(dyn), 'Platzierung behaelt', 'Platzierungen behalten')} "
                   "ihre Attribute als Daten, die Formeln an der Definition liest OpenSKP nicht, "
                   f"das dynamische Verhalten geht verloren: {_namen(dyn)}")
    lost = stats.get("attribute_lost", 0)
    if lost:
        out.append(f"Attribute: {_mehrzahl(lost, 'Wert geht', 'Werte gehen')} verloren "
                   f"(Text ueber {_STR_MAX} Zeichen oder unbekannter Typ)")
    n = sum(1 for i in insts if getattr(model.definitions.get(i.ref_idx), "is_image", False))
    if n:
        out.append(f"Bilder: {_mehrzahl(n, 'Bild wird', 'Bilder werden')} als Komponente mit Bildtextur "
                   "geschrieben, nicht als SketchUp-Bildobjekt")
    return out


def rewrite_legacy(src: Path, out: Path) -> dict:
    """Beliebige lesbare .skp (auch 2021+) als SketchUp-2017-Datei neu aufbauen.

    Nutzt die Daten-Wiedergabe aus openskp.edit (Materialien, Ebenen, Komponenten,
    Gruppen, Instanzen), ohne deren Sperre fuer Dateien ab 2021. Dazu kommen freie Texte und
    Bemassungen (in ihrer Definition), alle Attribut-Woerterbuecher der Platzierungen und je
    verlorener Art eine Zeile in stats["verluste"] (auch in stats["warnings"]). Es wird kein Code
    aus der Datei erzeugt oder ausgefuehrt. openskp ist auf 1.3.0 gepinnt, weil hier
    interne Funktionen genutzt werden.
    """
    skp = open_skp(src)
    model = model_of(skp)
    notes = anmerkungen(model, skp._parsed)
    skp._parsed = None  # Rohergebnis nur fuer die Endpunkte der Bemassungen, danach freigeben
    # Materialien ohne ID sind von keiner Flaeche referenzierbar (z. B. "Layer_Layer0" von
    # Render-Plugins). Mitkopiert wuerden sie echte IDs bekommen und Ebenenfarben imitieren.
    model = dataclasses.replace(model, materials=[mt for mt in model.materials if mt.id is not None])
    warnings: list[str] = []
    stats = {"triangulated": 0, "skipped": 0, "texts": 0, "dimensions": 0}
    builder = create()

    def notes_of(key):
        return notes.texte.get(key, ()), notes.masse.get(key, ())

    with tolerant_faces(stats):
        material_slots = _replay_materials(builder, model, warnings)
        layer_slots = {
            layer.name: builder.add_layer(layer.name, color=(layer.color_r, layer.color_g, layer.color_b),
                                          hidden=layer.hidden)
            for layer in model.layers
        }
        def_builders: dict = {}
        for def_id in _edit._definition_order(model):
            defn = model.definitions[def_id]
            context = f"definition {defn.name or def_id!r}"
            if not (_edit._definition_has_content(defn, def_builders) or any(_edge_points(defn))
                    or any(notes_of(def_id))):
                warnings.append(f"{context}: uebersprungen (keine Geometrie)")
                continue
            # Verhalten "immer zur Kamera drehen" (2D-Personen, Baeume) und "Schatten zur
            # Sonne": seit OpenSKP 1.3.0 schreibbar, wie in edit.open_existing
            with builder.add_component_definition(
                    defn.name, always_faces_camera=bool(getattr(defn, "always_faces_camera", False)),
                    shadows_face_sun=bool(getattr(defn, "shadows_face_sun", False))) as db:
                _replay_body(db, defn, model, material_slots, layer_slots, warnings, context,
                             def_builders, stats, builder, notes_of(def_id))
            def_builders[def_id] = db
        _replay_body(builder, model.root, model, material_slots, layer_slots, warnings, "root",
                     def_builders, stats, builder, notes_of("ROOT"))
    verluste = verlust_zeilen(model, notes, stats)
    warnings += verluste
    out.parent.mkdir(parents=True, exist_ok=True)
    save_atomic(builder, out)
    check = SkpFile.open(str(out)).parse()
    stats.update(
        version=check.version,
        faces_source=sum(len(d.faces) for d in model.definitions.values()) + len(model.root.faces),
        faces_written=sum(len(d.faces) for d in check.definitions.values()) + len(check.root.faces),
        components=len(check.definitions),
        materials=len(check.materials),
        layers=len(check.layers),
        warnings=warnings,
        verluste=verluste,
    )
    return stats


# ---------------------------------------------------------------- Blender-Dump -> SKP

def _clean_polygon(points, eps=1e-7):
    out = []
    for p in points:
        if not out or max(abs(p[k] - out[-1][k]) for k in range(3)) > eps:
            out.append(p)
    if len(out) > 1 and max(abs(out[0][k] - out[-1][k]) for k in range(3)) <= eps:
        out.pop()
    if len(out) < 3:
        return None
    nx, ny, nz = _newell(out)  # Newell-Normale: Flaeche ~0 -> degeneriert
    if math.sqrt(nx * nx + ny * ny + nz * nz) < 1e-10:
        return None
    return out


def _uv_pins(points, uvs):
    """Drei Texturpunkte, die ein moeglichst grosses Dreieck aufspannen (stabile Loesung).

    Die fruehere feste Wahl (Ecken 0, n/3, 2n/3) konnte drei Punkte auf einer Geraden treffen,
    dann war keine Texturlage berechenbar und die Flaeche fiel weg."""
    n = len(points)
    if n < 3:
        return None
    pts = np.asarray(points, np.float64)
    i0 = 0
    i1 = int(np.argmax(np.linalg.norm(pts - pts[i0], axis=1)))
    area = np.linalg.norm(np.cross(pts[i1] - pts[i0], pts - pts[i0]), axis=1)
    i2 = int(np.argmax(area))
    if area[i2] < 1e-12 or len({i0, i1, i2}) < 3:
        return None
    return [(tuple(points[i]), (float(uvs[i][0]), float(uvs[i][1]))) for i in (i0, i1, i2)]


# ---------------------------------------------------------------- Binaer-Dump aus Blender -> SKP

DUMP_FORMAT = "skptool-dump-bin"


def read_dump_header(header_path: Path) -> dict:
    header = json.loads(Path(header_path).read_text(encoding="utf-8"))
    if header.get("format") != DUMP_FORMAT or header.get("version") not in (1, 2, 3, 4):
        raise ValueError(f"{header_path}: kein skptool-Binaerdump (Version 1 bis 4)")
    return header


def iter_dump_definitions(header: dict, bin_path: Path, only=None):
    """(Index, Eintrag, verts float32 (n,3), ltotal, lverts, pmat, uv float32 (k,2)) je Definition.

    Liest objektweise per seek + np.fromfile, der Speicher waechst nicht mit der Dateigroesse.
    """
    with open(bin_path, "rb") as fh:
        for i, d in enumerate(header["definitions"]):
            if only is not None and i not in only:
                continue
            yield (i, d, *_read_dump_definition(fh, d))


def _read_dump_definition(fh, d):
    fh.seek(d["offset"])
    verts = np.fromfile(fh, np.float32, d["nverts"] * 3).reshape(-1, 3)
    lt = np.fromfile(fh, np.int32, d["npolys"])
    lv = np.fromfile(fh, np.int32, d["nloops"])
    pm = np.fromfile(fh, np.int32, d["npolys"])
    uv = np.fromfile(fh, np.float32, d["nuv"] * 2).reshape(-1, 2)
    if "nhard" in d:  # Dump-Version 2: harte Kanten + Rueckseitenmaterial
        hard = np.fromfile(fh, np.int32, d["nhard"] * 2).reshape(-1, 2)
        pback = np.fromfile(fh, np.int32, d["npolys"])
    else:
        hard, pback = None, None
    buv = np.fromfile(fh, np.float32, d.get("nbuv", 0) * 2).reshape(-1, 2)
    return verts, lt, lv, pm, uv, hard, pback, buv


def _definition_faces(verts, lt, lv, pm, uv, factor, textured, stats, pback=None, buv=None):
    """Polygone einer Definition fuer den Writer aufbereiten.

    Rueckgabe: Liste (pts, material_index, uv_or_None, same_len). Gleiche Reihenfolge und
    gleiche Werte wie der JSON-Weg (float32 -> float64 -> * factor).
    """
    v64 = verts.astype(np.float64) * factor
    if len(v64) and (not np.isfinite(v64).all() or np.abs(v64).max() > MAX_INCHES):
        raise ValueError("Das Modell enthaelt ungueltige Koordinaten (NaN, unendlich oder weiter als "
                         "1000 km vom Ursprung), nichts geschrieben")
    vl = list(map(tuple, v64.tolist()))
    lvl, ltl, pml = lv.tolist(), lt.tolist(), pm.tolist()
    pbl = pback.tolist() if pback is not None else [-1] * len(ltl)
    uvl = uv.astype(np.float64).tolist() if len(uv) else None
    buvl = buv.astype(np.float64).tolist() if buv is not None and len(buv) else None
    faces = []
    pos = upos = bpos = 0
    for n, m, mb in zip(ltl, pml, pbl):
        idx = lvl[pos:pos + n]
        pos += n
        f_uv = None
        if uvl is not None and m >= 0 and textured[m]:
            f_uv = uvl[upos:upos + n]
            upos += n
        b_uv = None
        if buvl is not None and mb >= 0 and textured[mb]:
            b_uv = buvl[bpos:bpos + n]
            bpos += n
        raw = [vl[i] for i in idx]
        pts = _clean_polygon(raw)
        if pts is None:
            stats["skipped"] += 1
            continue
        if f_uv is not None:  # Zuordnung ueber die Position, Aufraeumen kostet keine Textur
            by_point = dict(zip(raw, f_uv))
            f_uv = [by_point[p] for p in pts]
        if b_uv is not None:
            by_point = dict(zip(raw, b_uv))
            b_uv = [by_point[p] for p in pts]
        faces.append((pts, m, f_uv, True, mb, b_uv))
    return faces, vl


def _predeclare_hard_edges(target, faces, hard_pts, stats):
    """harte Kanten vor den Flaechen als eigene Kanten anlegen.

    Der Writer vergibt weich/glatt/verborgen nur an Kanten, die ein add_face-Aufruf NEU anlegt.
    Harte Kanten zuerst (ohne Flag) anlegen, dann alle Flaechen mit soft/smooth schreiben: die
    harten bleiben hart, alle uebrigen (auch Triangulierungsdiagonalen) werden weich."""
    writer = target._skp._definition_writer
    seen = set()
    for f in faces:
        pts = f[0]
        for i in range(len(pts)):
            p, q = pts[i], pts[(i + 1) % len(pts)]
            k = frozenset((p, q))
            if k in hard_pts and k not in seen:
                seen.add(k)
                _, _, new = writer._write_edge_chain([p, q], target._vertex_slots, target._edge_registry, False)
                target._new_entity_count += new
    stats["hard_edges"] = stats.get("hard_edges", 0) + len(seen)


def _texture_pins(pts, uvs, stats):
    """Texturpunkte fuer eine Flaeche: drei Punkte, oder alle Ecken, wenn die Texturlage nicht
    affin ist (verzerrte Textur, SketchUps "fixierte Pins")."""
    pins = _uv_pins(pts, uvs)
    if pins and len(pts) >= 4:
        affine = _uv_from_pins(pins, pts)
        if affine is None or max(abs(affine[p][k] - t[k]) for p, t in zip(pts, uvs) for k in (0, 1)) > 1e-4:
            pins = [(p, (float(t[0]), float(t[1]))) for p, t in zip(pts, uvs)]
            stats["perspective"] = stats.get("perspective", 0) + 1
    return pins


def _write_faces(target, faces, mat_ids, stats, hard_pts=None):
    add = ComponentDefinitionBuilder.add_face
    if hard_pts is not None:
        _predeclare_hard_edges(target, faces, hard_pts, stats)
    for pts, m, f_uv, same_len, mb, b_uv in faces:
        mid = mat_ids[m] if 0 <= m < len(mat_ids) else None
        kwargs = {"material": mid}
        if hard_pts is not None:
            kwargs.update(soft_edges=True, smooth_edges=True)
        bid = mat_ids[mb] if 0 <= mb < len(mat_ids) else None
        if bid is not None:
            kwargs["back_material"] = bid
        uv_lookup = None
        if mid is not None and f_uv is not None and same_len:
            pins = _texture_pins(pts, f_uv, stats)
            if pins:
                kwargs["front_uv"] = pins
                uv_lookup = dict(zip(pts, f_uv))
        if bid is not None and b_uv is not None:
            bpins = _texture_pins(pts, b_uv, stats)
            if bpins:
                kwargs["back_uv"] = bpins
        elif bid is not None and bid == mid and "front_uv" in kwargs:
            kwargs["back_uv"] = kwargs["front_uv"]  # gleiche Textur hinten: gleiche Stecknadeln
        before = stats["skipped"]
        add_face_safe(add, target, pts, stats, uv_lookup=uv_lookup, **kwargs)
        if stats["skipped"] > before:
            continue
        stats["faces"] += 1


MAX_INCHES = 1e6 / INCH  # 1000 km in Zoll


def _placement(matrix16, factor):
    """Blender-Weltmatrix (zeilenweise, Meter) -> (translation in Zoll, matrix3x3 oder None).

    openskp erwartet matrix3x3 zeilenweise mit Welt = M @ v + t (empirisch geprueft:
    matrix_convention_test.py, auch fuer gedrehte, ungleichmaessig skalierte Instanzen).
    Identitaet -> None, damit Gruppen byte-gleich zum alten Weg geschrieben werden.
    """
    m = np.asarray(matrix16, np.float64).reshape(4, 4)
    if not np.isfinite(m).all() or np.abs(m[:3, 3]).max() * factor > MAX_INCHES:
        raise ValueError("Das Modell enthaelt ungueltige Positionen (NaN, unendlich oder weiter als "
                         "1000 km vom Ursprung), nichts geschrieben")
    t = tuple(float(x) * factor for x in m[:3, 3])
    lin = m[:3, :3]
    m9 = None if np.array_equal(lin, np.eye(3)) else tuple(lin.reshape(-1).tolist())
    return t, m9


_BLENDER_SUFFIX = re.compile(r"\.\d{3}$")  # Blender haengt an doppelte Namen ".001" an


# So nennt SketchUp die Definitionen von Gruppen ("Group#12", deutsch "Gruppieren#3", "#4")
_GROUP_NAME = re.compile(r"^(?:group|gruppe|gruppieren|groupe|grupo|gruppo|groep)?\d*(?:#\d+)?$", re.I)


def _is_component_name(name):
    """Definition war in SketchUp eine Komponente (eigener Name) und keine Gruppe."""
    return bool(name) and not _GROUP_NAME.match(name.strip())


def _definition_name(rep, header, used):
    """Name einer Definition: SketchUp-Name aus dem Import, sonst Mesh-/Objektname. Komponenten
    (used ist ein Set) bekommen eindeutige Namen wie in SketchUp: "Stuhl", "Stuhl#2", ..."""
    name = rep.get("skp_definition") or ""
    if not name and rep["definition"] >= 0:
        name = header["definitions"][rep["definition"]]["name"]
    name = _BLENDER_SUFFIX.sub("", name or rep["name"]) or "Gruppe"
    if used is None:
        return name
    base, n = name, 1
    while name in used:
        n += 1
        name = f"{base}#{n}"
    used.add(name)
    return name


_MATRIX_TOL = 5e-5  # Meter bzw. Faktor: Rechenrauschen aus Blender (einige Mikrometer) gilt als gleich


def _coarse_matrix(m):
    """Grobe Form einer Matrix fuer den Vorab-Vergleich (1 cm, 1/1000)."""
    return tuple(round(v, 2 if j in (3, 7, 11) else 3) + 0.0 for j, v in enumerate(m[:12]))


def _matrices_close(a, b):
    for x, y in zip(a, b):
        for u, v in zip(x, y):
            if abs(u - v) > _MATRIX_TOL + 1e-6 * abs(u):
                return False
    return True


def _nesting_plan(instances):
    """Verschachtelung fuer den Writer: gleiche Teilbaeume werden eine Definition.

    Signatur eines Objekts = (Mesh-Definition, sortierte Kinder als (Kind-Signatur, Name, Matrix,
    Ebene, verborgen, Material)). Gleiche Signatur heisst gleicher SketchUp-Definitionsinhalt;
    Matrizen gelten bis _MATRIX_TOL als gleich (geschrieben werden die des ersten Objekts).
    Rueckgabe: sig je Instanz, children je Instanz, users je Signatur, count je Signatur und
    order = Signaturen so sortiert, dass jede Definition nach allen kommt, die sie enthaelt."""
    n = len(instances)
    children = [[] for _ in range(n)]
    roots = []
    for i, inst in enumerate(instances):
        p = inst.get("parent", -1)
        (children[p] if 0 <= p < n and p != i else roots).append(i)
    # Nachordnung ohne Rekursion; Zyklen (kaputte Eltern-Angaben) werden flach an die Wurzel gelegt
    post, state = [], [0] * n
    for r in roots:
        stack = [(r, False)]
        while stack:
            i, done = stack.pop()
            if done:
                post.append(i)
                state[i] = 2
                continue
            if state[i]:
                continue
            state[i] = 1
            stack.append((i, True))
            stack.extend((k, False) for k in reversed(children[i]) if not state[k])
    if len(post) < n:
        reached = set(post)
        for i in range(n):
            if i not in reached:
                instances[i]["parent"] = -1
                children[i] = []
                post.append(i)
        for i in range(n):
            children[i] = [k for k in children[i] if instances[k].get("parent", -1) == i]
    sig = [0] * n
    depth = [0] * n
    users: dict = {}
    order = []
    buckets: dict = {}  # grober Schluessel -> [(sig, Matrizen der Kinder)]
    for i in post:
        kids = []
        for k in children[i]:
            c = instances[k]
            m = c["matrix"]
            kids.append(((sig[k], _BLENDER_SUFFIX.sub("", c["name"]), c["layer"], bool(c.get("hidden")),
                          c.get("material", -1), _coarse_matrix(m)), m))
        kids.sort(key=lambda kv: (kv[0], kv[1]))
        key = (instances[i]["definition"], tuple(kv[0] for kv in kids))
        mats = [kv[1] for kv in kids]
        cands = buckets.setdefault(key, [])
        s_id = next((c_id for c_id, c_mats in cands if _matrices_close(mats, c_mats)), None)
        if s_id is None:
            s_id = len(users)
            users[s_id] = []
            order.append(s_id)
            cands.append((s_id, mats))
        sig[i] = s_id
        users[s_id].append(i)
        depth[i] = 1 + max((depth[k] for k in children[i]), default=0)
    return {"sig": sig, "children": children, "users": users, "order": order,
            "count": {s_id: len(u) for s_id, u in users.items()}, "depth": max(depth, default=0)}


def _dump_colorize(value):
    """Zielfarbe einer getoenten Textur aus dem Dump-Kopf (bridge._MaterialTable) oder None."""
    if not isinstance(value, dict):
        return None
    rgb = value.get("rgb")
    if not isinstance(rgb, list) or len(rgb) != 3 or not all(isinstance(c, int) for c in rgb):
        return None
    return [max(0, min(255, c)) for c in rgb]


def write_skp_from_bin(header_path: Path, out: Path, scale_to_inch: float | None = None,
                       textures: bool = True, bin_path: Path | None = None, reparse: bool = True) -> dict:
    """Binaer-Dump aus Blender als SketchUp-2017-Datei schreiben, Instanzen bleiben erhalten.

    Die Blender-Hierarchie (Eltern/Kinder) wird zu verschachtelten Definitionen, gleiche
    Teilbaeume teilen sich eine. Mehrfach benutzt oder in SketchUp benannt -> Komponente, sonst
    Gruppe.
    Jede Collection wird ein Tag (Ebene).
    """
    header_path = Path(header_path)
    header = read_dump_header(header_path)
    bin_path = Path(bin_path) if bin_path else header_path.with_name(header["bin"])
    unit = header.get("unit", "m")
    factor = scale_to_inch if scale_to_inch else (1.0 / INCH if unit == "m" else 1.0)
    b = create()
    stats = {"groups": 0, "components": 0, "instances": 0, "faces": 0, "triangulated": 0,
             "skipped": 0, "textured_materials": 0}

    # Reihenfolge ist Pflicht im Writer: Materialien, Ebenen, Definitionen, dann Instanzen
    mat_ids = []
    used_names: set = set()
    for mt in header["materials"]:
        name = mt["name"]
        n = 1
        while name in used_names:
            n += 1
            name = f"{mt['name']}_{n}"
        used_names.add(name)
        alpha = float(mt.get("alpha", 1.0))
        opacity = None if alpha >= 0.999 else max(0.0, min(1.0, alpha))
        if textures and mt.get("image") and Path(mt["image"]).exists():
            mat_ids.append(b.add_texture_material(name, mt["image"], opacity=opacity))
            tint = _dump_colorize(mt.get("colorize"))
            if tint is not None and set_texture_colorize(b, tint):
                stats["colorized_materials"] = stats.get("colorized_materials", 0) + 1
            else:
                set_texture_average_color(b, image_average_rgb(mt["image"]))
            stats["textured_materials"] += 1
        else:
            mat_ids.append(b.add_material(name, [int(c) for c in mt["rgba"][:3]] + [255], opacity=opacity))
    textured = [bool(mt.get("image")) for mt in header["materials"]]

    instances = header["instances"]
    layer_ids = {}
    all_layers = {o["layer"] for o in instances if o["layer"]} | set(header.get("layers", []))
    hidden_layers = set(header.get("hidden_layers", []))
    for name in sorted(all_layers):  # auch Ebenen ohne Objekte, sie gehoeren zum Modell
        if name in ("Layer0", "Untagged", "Scene Collection"):
            continue
        layer_ids[name] = b.add_layer(name, hidden=name in hidden_layers)

    def inst_kwargs(inst):
        t, m9 = _placement(inst["matrix"], factor)
        mat = inst.get("material", -1)
        return {"name": _BLENDER_SUFFIX.sub("", inst["name"]), "translation": t, "matrix3x3": m9,
                "material": mat_ids[mat] if 0 <= mat < len(mat_ids) else None,
                "layer": layer_ids.get(inst["layer"]), "hidden": inst.get("hidden", False)}

    plan = _nesting_plan(instances)
    stats["nested_levels"] = plan["depth"]
    built: dict = {}  # Signatur -> geschlossener Builder oder None (leer bzw. Wurzelgruppe)
    comp_names: set = set()
    is_component: dict = {}
    root_components = []  # (builder, instanz) - Platzierung erst nach allen Definitionen
    with open(bin_path, "rb") as fh:
        for sig in plan["order"]:
            users = plan["users"][sig]
            rep = instances[users[0]]
            faces, hard_pts = [], None
            if rep["definition"] >= 0:
                verts, lt, lv, pm, uv, hard, pback, buv = _read_dump_definition(
                    fh, header["definitions"][rep["definition"]])
                faces, vl = _definition_faces(verts, lt, lv, pm, uv, factor, textured, stats, pback, buv)
                if hard is not None:
                    hard_pts = {frozenset((vl[a], vl[c])) for a, c in hard.tolist()}
                del verts, lt, lv, pm, uv, vl
            kids = [(built[plan["sig"][k]], k) for k in plan["children"][users[0]]
                    if built.get(plan["sig"][k]) is not None]
            if not faces and not kids:
                built[sig] = None
                continue
            component = len(users) > 1 or _is_component_name(rep.get("skp_definition"))
            is_component[sig] = component
            root_group = not component and rep["parent"] < 0
            if root_group:
                ctx = b.add_group(**inst_kwargs(rep))
                stats["groups"] += 1
            else:
                ctx = b.add_component_definition(
                    _definition_name(rep, header, comp_names if component else None))
                stats["components" if component else "groups"] += 1
            with ctx as target:
                if faces:
                    _write_faces(target, faces, mat_ids, stats, hard_pts)
                for kid, k in kids:
                    if is_component[plan["sig"][k]]:
                        target.add_instance(kid, **inst_kwargs(instances[k]))
                        stats["instances"] += 1
                    else:
                        target.add_group_instance(kid, **inst_kwargs(instances[k]))
            del faces
            built[sig] = None if root_group else target
            if component:
                root_components += [(target, instances[u]) for u in users if instances[u]["parent"] < 0]
    for cdef, inst in root_components:
        b.add_instance(cdef, **inst_kwargs(inst))
        stats["instances"] += 1
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    save_atomic(b, out)
    if reparse:
        check = SkpFile.open(str(out)).parse()
        stats["version"] = check.version
        stats["faces_reparsed"] = sum(len(d.faces) for d in check.definitions.values()) + len(check.root.faces)
        stats["faces_reparsed_placed"] = placed_face_count(check)
    return stats


def placed_face_count(model) -> int:
    """Flaechen wie platziert (Definition-Flaechen x Instanzanzahl, rekursiv)."""
    memo: dict = {}

    def count(defn, depth=0):
        key = id(defn)
        if key in memo:
            return memo[key]
        total = len(defn.faces)
        if depth < 64:
            for inst in defn.instances:
                ref = model.definitions.get(inst.ref_idx)
                if ref is not None:
                    total += count(ref, depth + 1)
        memo[key] = total
        return total

    return count(model.root)
