"""Bearbeitungsoperationen fuer SketchUp-Modelle in Blender (laeuft INNERHALB von Blender).

Eine feste Liste von Operationen, als JSON beschreibbar. Genutzt von:
  - bridge.py (Hintergrund: skptool edit / skptool list)
  - dem Live-Add-on im offenen Blender-Fenster

Bewusst gibt es keine Operation, die beliebigen Code ausfuehrt.

Jede Operation ist ein dict mit "op" und meist einer Auswahl:
  {"op": "move", "select": {"name": "Palme*"}, "by": [0, 0, 0.5]}

Auswahl ("select"), alle Angaben optional und kombinierbar (UND):
  name        Muster mit * und ?, ohne Gross-/Kleinschreibung, auf den Objektnamen
  layer       Name der Ebene (Collection)
  material    Objekt benutzt dieses Material
  definition  Name der SketchUp-Komponente (alle Platzierungen)
  type        "mesh" (Standard) oder "any"
Ohne "select" gilt die Operation fuer alle Mesh-Objekte (nur bei list und hide/show sinnvoll).

Einheiten: Meter, Grad. Achsen: x, y, z wie in SketchUp (z oben).

Grenzen (gegen versehentliche oder boeswillige Riesenauftraege): hoechstens MAX_OPS Operationen
pro Aufruf, hoechstens MAX_OBJECTS Objekte in der Szene, Zahlen endlich und betragsmaessig bis
MAX_COORD, Namen bis 63 Zeichen ohne Steuerzeichen.
"""
import fnmatch
import math
import unicodedata

import bpy
from mathutils import Matrix, Vector

OPS = {}
MAX_OPS = 1000
MAX_OBJECTS = 100_000
MAX_COORD = 1e6        # Meter bzw. Faktor
MAX_PATTERN = 256


def op(fn=None, *, name=None):
    """Operation registrieren; der JSON-Name ist der Funktionsname oder name=..."""
    def register(f):
        OPS[name or f.__name__] = f
        return f
    return register(fn) if fn is not None else register


class OpError(ValueError):
    pass


# ---------------------------------------------------------------- Auswahl

def _collections_of(ob):
    return [c.name for c in ob.users_collection]


def _materials_of(ob):
    return sorted({_base(s.material.name) for s in ob.material_slots if s.material})


def _suffix(name):
    return len(name) > 4 and name[-4] == "." and name[-3:].isdigit()


def _base(name):
    return name[:-4] if _suffix(name) else name


def _text(v, what, max_len=MAX_PATTERN):
    if not isinstance(v, str):
        raise OpError(f"{what} muss ein Text sein")
    if len(v) > max_len:
        raise OpError(f"{what} ist zu lang (hoechstens {max_len} Zeichen)")
    if any(unicodedata.category(ch) == "Cc" or 0x80 <= ord(ch) <= 0x9F for ch in v):
        raise OpError(f"{what} enthaelt Steuerzeichen")
    return v


def _name(v, what):
    """Name fuer Objekt, Ebene oder Material: 1 bis 63 Zeichen (Blender-Grenze), ohne Steuerzeichen."""
    v = _text(v, what, 63).strip()
    if not v:
        raise OpError(f"{what} darf nicht leer sein")
    return v


def select(spec=None):
    if spec is None:
        spec = {}
    if not isinstance(spec, dict):
        raise OpError('select muss ein Objekt sein, z. B. {"name": "Palme*"}')
    unknown = set(spec) - {"name", "layer", "material", "definition", "type"}
    if unknown:
        raise OpError(f"Unbekannte Auswahlfelder: {', '.join(sorted(map(str, unknown)))}")
    for key in ("name", "layer", "material", "definition"):
        if key in spec:
            _text(spec[key], f"select.{key}")
    kind = spec.get("type", "mesh")
    if kind not in ("mesh", "any"):
        raise OpError('select.type muss "mesh" oder "any" sein')
    obs = [o for o in bpy.context.scene.objects if kind == "any" or o.type == "MESH"]
    if "name" in spec:
        pat = spec["name"].lower()
        obs = [o for o in obs if fnmatch.fnmatchcase(o.name.lower(), pat)
               or fnmatch.fnmatchcase(_base(o.name).lower(), pat)]
    if "layer" in spec:
        obs = [o for o in obs if spec["layer"] in _collections_of(o)]
    if "material" in spec:
        want = spec["material"]
        obs = [o for o in obs if any(s.material and _base(s.material.name) == want for s in o.material_slots)]
    if "definition" in spec:
        want = spec["definition"]
        obs = [o for o in obs if str(o.get("skp_definition", "")) == want
               or (o.type == "MESH" and _base(o.data.name) == want)]
    return obs


def _require(obs, spec):
    if not obs:
        raise OpError(f"Keine Objekte passen zur Auswahl {spec}")
    return obs


# ---------------------------------------------------------------- Hilfen

def _world_bbox(obs):
    mn = Vector((math.inf,) * 3)
    mx = Vector((-math.inf,) * 3)
    for o in obs:
        for c in o.bound_box:
            w = o.matrix_world @ Vector(c)
            mn = Vector(map(min, mn, w))
            mx = Vector(map(max, mx, w))
    return mn, mx


def _pivot(obs, pivot):
    if pivot in (None, "self", "each", "bottom"):
        return None  # je Objekt eigene Mitte (bei "bottom" Mitte der Unterkante)
    if pivot == "group":
        mn, mx = _world_bbox(obs)
        return (mn + mx) / 2
    if pivot == "origin":
        return Vector((0, 0, 0))
    if isinstance(pivot, (list, tuple)) and len(pivot) == 3:
        return _vec(pivot, "pivot")
    raise OpError(f"Unbekannter Drehpunkt {pivot!r} (self, group, origin oder [x, y, z])")


def _apply_world(ob, m_world):
    """Weltmatrix setzen, auch fuer Kinder in einer Hierarchie."""
    ob.matrix_world = m_world @ ob.matrix_world


def _around(center, m):
    return Matrix.Translation(center) @ m @ Matrix.Translation(-center)


def _num(v, name, lo=-MAX_COORD, hi=MAX_COORD):
    """Endliche Zahl in [lo, hi]; keine Wahrheitswerte, keine Texte, kein NaN/Unendlich."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise OpError(f"{name} muss eine Zahl sein")
    v = float(v)
    if not math.isfinite(v) or not lo <= v <= hi:
        raise OpError(f"{name} muss zwischen {lo:g} und {hi:g} liegen")
    return v


def _int(v, name, lo, hi):
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise OpError(f"{name} muss eine ganze Zahl zwischen {lo} und {hi} sein")
    return v


def _vec(v, name, lo=-MAX_COORD, hi=MAX_COORD):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        x = _num(v, name, lo, hi)
        return Vector((x, x, x))
    if isinstance(v, (list, tuple)) and len(v) == 3:
        return Vector([_num(x, name, lo, hi) for x in v])
    raise OpError(f"{name} braucht eine Zahl oder [x, y, z]")


def _color(c):
    """[r, g, b] als 0 bis 255 oder 0 bis 1 (alle Werte hoechstens 1)."""
    if not isinstance(c, (list, tuple)) or len(c) not in (3, 4):
        raise OpError("color braucht [r, g, b], Werte 0 bis 255 (oder 0 bis 1)")
    vals = [_num(x, "color", 0, 255) for x in c[:3]]
    return [x / 255 for x in vals] if max(vals) > 1 else vals


def _budget(extra):
    now = len(bpy.data.objects)
    if now + extra > MAX_OBJECTS:
        raise OpError(f"Das ergaebe {now + extra} Objekte, hoechstens {MAX_OBJECTS} sind erlaubt")


def _top_level(obs):
    """Objekte, deren Eltern nicht mit ausgewaehlt sind (sonst doppelte Transformation)."""
    chosen = set(obs)
    out = []
    for o in obs:
        p = o.parent
        while p is not None and p not in chosen:
            p = p.parent
        if p is None:
            out.append(o)
    return out


def _info(o):
    mn, mx = _world_bbox([o])
    size = mx - mn
    return {
        "name": o.name,
        "layer": ", ".join(_collections_of(o)),
        "definition": str(o.get("skp_definition", "")) or (_base(o.data.name) if o.type == "MESH" else ""),
        "placements": o.data.users if o.type == "MESH" else 0,
        "materials": _materials_of(o) if o.type == "MESH" else [],
        "center": [round(v, 3) for v in (mn + mx) / 2],
        "size": [round(v, 3) for v in size],
        "faces": len(o.data.polygons) if o.type == "MESH" else 0,
        "hidden": not o.visible_get(),
    }


def _material(name, color=None, alpha=None, create=True):
    name = _name(name, "material")
    rgb_in = _color(color) if color is not None else None
    alpha = _num(alpha, "alpha", 0, 1) if alpha is not None else None
    mat = bpy.data.materials.get(name)
    if mat is None:
        if not create:
            raise OpError(f"Material {name!r} gibt es nicht")
        if color is None:
            raise OpError(f"Neues Material {name!r} braucht eine Farbe, z. B. \"color\": [60, 60, 60]")
        mat = bpy.data.materials.new(name)
        mat.use_nodes = True
    if rgb_in is not None:
        rgb = rgb_in
        a = 1.0 if alpha is None else alpha
        mat.diffuse_color = (*rgb, a)
        node = next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None) if mat.use_nodes else None
        if node is not None:
            base = node.inputs["Base Color"]
            for link in list(base.links):  # Farbe statt Textur
                mat.node_tree.links.remove(link)
            base.default_value = (*rgb, 1.0)
            node.inputs["Alpha"].default_value = a
    elif alpha is not None:
        mat.diffuse_color = (*mat.diffuse_color[:3], float(alpha))
    return mat


# ---------------------------------------------------------------- Operationen

@op(name="list")
def list_objects(p):
    obs = select(p.get("select"))
    limit = _int(p.get("limit", 200), "limit", 1, 100_000)
    return {"count": len(obs), "objects": [_info(o) for o in obs[:limit]],
            "truncated": len(obs) > limit}


@op
def summary(p):
    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    mn, mx = _world_bbox(meshes) if meshes else (Vector(), Vector())
    return {
        "objects": len(meshes),
        "unique_meshes": len({o.data for o in meshes}),
        "size": [round(v, 3) for v in (mx - mn)],
        "layers": [{"name": c.name, "objects": len(c.objects), "hidden": c.hide_viewport}
                   for c in bpy.data.collections],
        "materials": sorted({_base(m.name) for m in bpy.data.materials if m.users}),
    }


@op
def move(p):
    obs = _top_level(_require(select(p.get("select")), p.get("select")))
    if "by" in p:
        d = _vec(p["by"], "by")
        for o in obs:
            _apply_world(o, Matrix.Translation(d))
    elif "to" in p:
        mn, mx = _world_bbox(obs)
        center = (mn + mx) / 2
        target = _vec(p["to"], "to")
        if p.get("anchor") == "bottom":
            center.z = mn.z
        for o in obs:
            _apply_world(o, Matrix.Translation(target - center))
    else:
        raise OpError("move braucht \"by\": [x, y, z] oder \"to\": [x, y, z]")
    return {"moved": len(obs)}


@op
def rotate(p):
    obs = _top_level(_require(select(p.get("select")), p.get("select")))
    axis = p.get("axis", "z")
    axis = axis.upper() if isinstance(axis, str) else ""
    if axis not in ("X", "Y", "Z"):
        raise OpError("axis muss x, y oder z sein")
    m = Matrix.Rotation(math.radians(_num(p["deg"], "deg", -36000, 36000)), 4, axis)
    pivot = p.get("pivot", "self")
    center = _pivot(obs, pivot)
    for o in obs:
        c = center
        if c is None:
            mn, mx = _world_bbox([o])
            c = (mn + mx) / 2
            if pivot == "bottom":
                c.z = mn.z
        _apply_world(o, _around(c, m))
    return {"rotated": len(obs)}


@op
def scale(p):
    obs = _top_level(_require(select(p.get("select")), p.get("select")))
    f = _vec(p["factor"], "factor", -1e4, 1e4)
    if min(abs(v) for v in f) < 1e-6:
        raise OpError("factor muss betragsmaessig mindestens 0.000001 sein")
    m = Matrix.Diagonal((*f, 1.0))
    pivot = p.get("pivot", "self")
    center = _pivot(obs, pivot)
    for o in obs:
        c = center
        if c is None:
            mn, mx = _world_bbox([o])
            c = (mn + mx) / 2
            if pivot == "bottom" or p.get("anchor") == "bottom":
                c.z = mn.z
        _apply_world(o, _around(c, m))
    return {"scaled": len(obs)}


@op
def set_material(p):
    obs = _require(select(p.get("select")), p.get("select"))
    mat = _material(p["material"], p.get("color"), p.get("alpha"))
    only = p.get("replace")  # nur dieses Material ersetzen, sonst alle Slots
    if only is not None:
        only = _name(only, "replace")
    n = 0
    for o in obs:
        if not o.material_slots:
            if o.data.users > 1:
                o.data = o.data.copy()  # eigene Geometrie, sonst aenderten sich alle Platzierungen
            o.data.materials.append(mat)
            n += 1
            continue
        for s in o.material_slots:
            if only is None or (s.material and _base(s.material.name) == only):
                s.link = "OBJECT"  # nur dieses Objekt, geteilte Geometrie bleibt unberuehrt
                s.material = mat
                n += 1
    return {"slots_changed": n, "objects": len(obs)}


@op
def recolor(p):
    name = _name(p["material"], "material")
    mats = [m for m in bpy.data.materials if _base(m.name) == name]
    if not mats:
        raise OpError(f"Material {name!r} gibt es nicht")
    for m in mats:
        _material(m.name, p.get("color"), p.get("alpha"), create=False)
    return {"materials_changed": len(mats)}


@op
def set_layer(p):
    obs = _require(select(p.get("select")), p.get("select"))
    name = _name(p["layer"], "layer")
    col = bpy.data.collections.get(name)
    if col is None:
        col = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(col)
    for o in obs:
        for c in list(o.users_collection):
            c.objects.unlink(o)
        col.objects.link(o)
    return {"moved_to_layer": len(obs), "layer": name}


def _layer_visibility(name, hidden):
    name = _name(name, "layer")
    col = bpy.data.collections.get(name)
    if col is None:
        raise OpError(f"Ebene {name!r} gibt es nicht")
    col.hide_viewport = hidden
    col.hide_render = hidden
    return {"layer": name, "hidden": hidden}


@op
def hide_layer(p):
    return _layer_visibility(p["layer"], True)


@op
def show_layer(p):
    return _layer_visibility(p["layer"], False)


@op
def delete(p):
    obs = _top_level(_require(select(p.get("select")), p.get("select")))
    doomed = [o for root in obs for o in _subtree(root)]  # Gruppe samt Inhalt, wie in SketchUp
    names = [o.name for o in obs]
    for o in reversed(doomed):
        bpy.data.objects.remove(o, do_unlink=True)
    return {"deleted": len(doomed), "names": names[:50]}


@op
def rename(p):
    obs = _require(select(p.get("select")), p.get("select"))
    if len(obs) != 1:
        raise OpError(f"rename braucht genau ein Objekt, gefunden: {len(obs)}")
    old = obs[0].name
    obs[0].name = _name(p["to"], "to")
    return {"from": old, "to": obs[0].name}


def _subtree(ob):
    """Objekt mit allen Unterobjekten (wie eine SketchUp-Gruppe mit ihrem Inhalt)."""
    out = [ob]
    for child in ob.children:
        out += _subtree(child)
    return out


@op
def duplicate(p):
    """Verknuepfte Kopien samt Inhalt (SketchUp: weitere Platzierungen derselben Komponente)."""
    obs = _top_level(_require(select(p.get("select")), p.get("select")))
    step = _vec(p.get("offset", [1, 0, 0]), "offset")
    count = _int(p.get("count", 1), "count", 1, 1000)
    _budget(sum(len(_subtree(r)) for r in obs) * count)
    made = []
    for root in obs:
        for i in range(1, count + 1):
            mapping = {}
            for o in _subtree(root):
                c = o.copy()  # teilt die Geometrie, behaelt lokale Transformation
                for col in o.users_collection:
                    col.objects.link(c)
                mapping[o] = c
            for o, c in mapping.items():
                if o is root:
                    c.parent = o.parent
                    c.matrix_world = Matrix.Translation(step * i) @ o.matrix_world
                else:
                    c.parent = mapping[o.parent]
                    c.matrix_parent_inverse = o.matrix_parent_inverse.copy()
            made.append(mapping[root].name)
    return {"created": len(made), "names": made[:50]}


@op
def add_box(p):
    size = _vec(p.get("size", [1, 1, 1]), "size", 1e-6, MAX_COORD)
    at = _vec(p.get("at", [0, 0, 0]), "at")  # Mitte der Unterseite
    name = _name(p.get("name", "Quader"), "name")
    _budget(1)
    me = bpy.data.meshes.new(name)
    x, y, z = size / 2
    v = [(-x, -y, 0), (x, -y, 0), (x, y, 0), (-x, y, 0),
         (-x, -y, size.z), (x, -y, size.z), (x, y, size.z), (-x, y, size.z)]
    f = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    me.from_pydata(v, [], f)
    ob = bpy.data.objects.new(name, me)
    ob.location = at
    col = bpy.context.scene.collection
    if p.get("layer"):
        layer = _name(p["layer"], "layer")
        col = bpy.data.collections.get(layer) or bpy.data.collections.new(layer)
        if col.name not in bpy.context.scene.collection.children:
            bpy.context.scene.collection.children.link(col)
    col.objects.link(ob)
    if p.get("material"):
        me.materials.append(_material(p["material"], p.get("color"), p.get("alpha")))
    return {"created": ob.name}


def run(operations, stop_on_error=True):
    """Liste von Operationen ausfuehren. Rueckgabe: Ergebnis je Operation."""
    if isinstance(operations, dict):
        operations = [operations]
    if not isinstance(operations, list):
        return [{"op": None, "ok": False, "index": 0,
                 "error": 'Operationen muessen eine Liste sein, z. B. [{"op": "summary"}]'}]
    if len(operations) > MAX_OPS:
        return [{"op": None, "ok": False, "index": 0,
                 "error": f"Hoechstens {MAX_OPS} Operationen pro Aufruf ({len(operations)} angegeben)"}]
    results = []
    for i, p in enumerate(operations):
        name = p.get("op") if isinstance(p, dict) else None
        name = name if isinstance(name, str) and len(name) <= 40 else None
        try:
            if not isinstance(p, dict):
                raise OpError("Jede Operation muss ein Objekt sein, z. B. {\"op\": \"summary\"}")
            fn = OPS.get(name)
            if fn is None:
                raise OpError(f"Unbekannte Operation {name!r}. Moeglich: {', '.join(sorted(OPS))}")
            results.append({"op": name, "ok": True, **fn(p)})
            bpy.context.view_layer.update()  # neue Positionen fuer die naechste Operation
        except Exception as exc:  # jeder Fehler endet hier, mit Meldung statt Absturz
            if isinstance(exc, OpError):
                msg = str(exc)
            elif isinstance(exc, KeyError):
                msg = f"fehlender Wert {exc}"
            else:
                msg = f"ungueltige Angabe ({type(exc).__name__}: {str(exc)[:200]})"
            results.append({"op": name, "ok": False, "error": msg, "index": i})
            if stop_on_error:
                break
    return results
