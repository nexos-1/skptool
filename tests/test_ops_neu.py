"""Tests fuer die Operationen align, distribute, array, hide/show, mirror und measure.

Start: .venv\\Scripts\\python -m unittest tests.test_ops_neu -v

Jede Operation laeuft ueber skptool edit auf samples/stuhl_tisch_2017.skp, das Ergebnis wird mit
core.model_of wieder eingelesen und numerisch geprueft (Platzierungen, Boxen, Flaechen).
Die Fehlerfaelle laufen zusaetzlich direkt gegen ops.run in einem Blender-Hintergrundprozess,
damit jede Meldung einzeln geprueft wird, ohne fuer jeden Fall die ganze Rundreise zu starten.
Ohne Blender werden die Tests uebersprungen.
"""
import contextlib
import io
import json
import math
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np

from skptool import cli, core
from skptool.blender import BlenderError, find_blender

ROOT = Path(__file__).resolve().parents[1]
S2017 = ROOT / "samples" / "stuhl_tisch_2017.skp"
SCRIPTS = ROOT / "skptool" / "blender_scripts"
IN = 0.0254   # Zoll in Meter
TOL = 1e-4    # Meter (0,1 mm)

try:
    BLENDER = find_blender()
except BlenderError:
    BLENDER = None


def run_cli(*args):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main(list(args))
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------- Modell auswerten

def placements(m):
    """Jede Platzierung mit Weltmatrix: {"root": Index der aeusseren Platzierung, "inst", "defn",
    "M": 3x3, "t": Verschiebung in Zoll, "depth"}. Reihenfolge wie im Modellbaum."""
    out = []

    def rec(d, M, t, root, depth):
        if depth > 64:
            return
        for inst in d.instances:
            ref = m.definitions.get(inst.ref_idx)
            if ref is None:
                continue
            A = np.array(inst.matrix[:9], np.float64).reshape(3, 3)
            b = np.array(inst.matrix[9:12], np.float64)
            p = {"root": len(out) if root is None else root, "inst": inst, "defn": ref,
                 "M": M @ A, "t": M @ b + t, "depth": depth}
            out.append(p)
            rec(ref, p["M"], p["t"], p["root"], depth + 1)

    rec(m.root, np.eye(3), np.zeros(3), None, 0)
    return out


def faces_of(p):
    """Flaechen einer Platzierung in Weltkoordinaten (Meter): Liste von (Punkte k x 3, Normale).
    Die Normale wird wie in SketchUp mit der inversen Transponierten mitgedreht."""
    d, M, t = p["defn"], p["M"], p["t"]
    nm = np.linalg.inv(M).T
    out = []
    for f in d.faces.values():
        pts = []
        for eid, orient in f.loops[0]:
            e = d.edges[eid]
            v = d.vertices[e.v1_id if orient == 1 else e.v2_id]
            pts.append((M @ np.array([v.x, v.y, v.z]) + t) * IN)
        n = nm @ np.array(f.normal, np.float64)
        out.append((np.array(pts), n / np.linalg.norm(n)))
    return out


def all_faces(pl, root=None):
    return [f for p in pl if root is None or p["root"] == root for f in faces_of(p)]


def box(faces):
    pts = np.concatenate([f[0] for f in faces])
    return pts.min(0), pts.max(0)


def roots_named(pl, name):
    return [i for i, p in enumerate(pl) if p["depth"] == 0 and p["defn"].name == name]


def match_faces(test, expected, got):
    """Jede erwartete Flaeche (Punkte, Normale) findet genau eine gleiche im Ergebnis."""
    test.assertEqual(len(expected), len(got))
    free = list(range(len(got)))
    for pts, n in expected:
        c = pts.mean(0)
        hit = None
        for k in free:
            q, nq = got[k]
            if (len(q) == len(pts) and np.linalg.norm(q.mean(0) - c) < TOL and float(n @ nq) > 0.999
                    and max(np.min(np.linalg.norm(q - v, axis=1)) for v in pts) < TOL):
                hit = k
                break
        test.assertIsNotNone(hit, f"keine passende Flaeche fuer Mitte {c.round(4)}, Normale {n.round(3)}")
        free.remove(hit)


def load(path):
    return core.model_of(core.open_skp(path))


# ---------------------------------------------------------------- ops.run direkt in Blender

HARNESS = r"""
import bpy, json, sys
argv = sys.argv[sys.argv.index("--") + 1:]
blend, scripts, cases_path, out_path = argv
bpy.ops.wm.open_mainfile(filepath=blend)
sys.path.insert(0, scripts)
import ops
with open(cases_path, encoding="utf-8") as fh:
    cases = json.load(fh)
res = []
for case in cases:
    before = len(bpy.data.objects)
    r = ops.run(case)
    bpy.context.view_layer.update()
    res.append({"results": r, "objects_before": before, "objects_after": len(bpy.data.objects)})
with open(out_path, "w", encoding="utf-8") as fh:
    json.dump(res, fh)
"""


@unittest.skipUnless(BLENDER, "Blender nicht installiert")
class TestNeueOperationen(unittest.TestCase):
    """Rundreisen ueber skptool edit. Das Original wird einmal fuer alle Tests eingelesen."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="skptool_opsneu_"))
        cls.orig = load(S2017)
        cls.orig_pl = placements(cls.orig)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def edit(self, ops, name):
        out = self.tmp / name
        code, text, err = run_cli("edit", str(S2017), "-o", str(out), "--ops", json.dumps(ops), "-q")
        self.assertEqual(code, 0, text + err)
        return load(out), text

    # ------------------------------------------------------------ Anordnen und Raster

    def test_align_distribute_array(self):
        ops = [
            # 1) alle Tischbeine in x und y auf die Mitte ihrer gemeinsamen Box
            {"op": "align", "select": {"name": "Leg_Table*"}, "axis": ["x", "y"], "to": "center"},
            # 2) Stuhl hinten buendig mit dem Tisch (groesstes y)
            {"op": "align", "select": {"name": "Chair"}, "axis": "y", "to": "max", "to_object": {"name": "Table"}},
            # 3) Stuhlbeine nach Mitten gleichmaessig in x verteilen
            {"op": "distribute", "select": {"name": "Leg_Chair*"}, "axis": "x"},
            # 4) Tisch und Stuhl mit 25 cm Luecke in x
            {"op": "distribute", "select": {"name": "*"}, "axis": "x", "gap": 0.25},
            # 5) Stuhl als Raster 3 x 2
            {"op": "array", "select": {"name": "Chair"}, "counts": [3, 2, 1], "spacing": [0.6, 0.5, 0]},
        ]
        m, text = self.edit(ops, "anordnen.skp")
        self.assertIn("aligned=4", text)
        self.assertIn("distributed=4", text)
        self.assertIn("gap=0.25", text)
        self.assertIn("created=5", text)
        pl = placements(m)
        # array: 6 Stuehle, eine gemeinsame Definition, 128 + 5 x 70 Flaechen
        chairs = roots_named(pl, "Chair")
        self.assertEqual(len(chairs), 6)
        self.assertEqual(len({pl[i]["inst"].ref_idx for i in chairs}), 1)
        self.assertEqual(core.placed_face_count(m), 128 + 5 * 70)
        ts = sorted((pl[i]["t"] * IN for i in chairs), key=lambda v: (round(v[1], 3), round(v[0], 3)))
        t0 = ts[0]
        want = sorted((t0 + np.array([i * 0.6, j * 0.5, 0.0]) for i in range(3) for j in range(2)),
                      key=lambda v: (round(v[1], 3), round(v[0], 3)))
        for a, b in zip(ts, want):
            self.assertLess(np.abs(a - b).max(), TOL, (a, b))
        first = next(i for i in chairs if np.abs(pl[i]["t"] * IN - t0).max() < TOL)
        (table,) = roots_named(pl, "Table")
        cmin, cmax = box(all_faces(pl, first))
        tmin, tmax = box(all_faces(pl, table))
        # align mit to_object: hintere Kante (max y) des Stuhls = die des Tisches
        self.assertAlmostEqual(cmax[1], tmax[1], delta=TOL)
        # distribute mit gap: Stuhl beginnt 25 cm hinter dem Tisch
        self.assertAlmostEqual(cmin[0] - tmax[0], 0.25, delta=TOL)
        # Hoehe unveraendert
        orig_chair = roots_named(self.orig_pl, "Chair")[0]
        omin, omax = box(all_faces(self.orig_pl, orig_chair))
        self.assertAlmostEqual(cmin[2], omin[2], delta=TOL)
        self.assertAlmostEqual(cmax[2], omax[2], delta=TOL)
        # Verschiebung des Stuhls genau um die berechneten Wege
        ot = self.orig_pl[orig_chair]["t"] * IN
        otable = roots_named(self.orig_pl, "Table")[0]
        o_tmin, o_tmax = box(all_faces(self.orig_pl, otable))
        self.assertAlmostEqual(t0[1] - ot[1], o_tmax[1] - omax[1], delta=TOL)
        self.assertAlmostEqual(t0[0] - ot[0], (o_tmax[0] + 0.25) - omin[0], delta=TOL)
        # distribute nach Mitten: Stuhlbeine bei -8, -8/3, 8/3, 8 Zoll (erstes und letztes bleiben)
        chair_def = pl[first]["defn"]
        legs = [i for i in chair_def.instances if m.definitions[i.ref_idx].name == "Leg_Chair"]
        xs = sorted(round(i.matrix[9], 3) for i in legs)
        self.assertEqual(xs, [-8.0, round(-8 / 3, 3), round(8 / 3, 3), 8.0])
        self.assertEqual(sorted(round(i.matrix[10], 3) for i in legs), [-7.5, -7.5, 7.5, 7.5])
        # align auf die Mitte: alle Tischbeine an derselben Stelle, in der Mitte ihrer alten Box
        table_def = pl[table]["defn"]
        tlegs = [i for i in table_def.instances if m.definitions[i.ref_idx].name == "Leg_Table"]
        self.assertEqual(len(tlegs), 4)
        spots = {(round(i.matrix[9], 3), round(i.matrix[10], 3), round(i.matrix[11], 3)) for i in tlegs}
        self.assertEqual(len(spots), 1, spots)
        leg_boxes = [box(faces_of(p)) for p in self.orig_pl if p["defn"].name == "Leg_Table"]
        want_c = (np.min([b[0] for b in leg_boxes], 0) + np.max([b[1] for b in leg_boxes], 0)) / 2
        got_boxes = [box(faces_of(p)) for p in pl if p["defn"].name == "Leg_Table"]
        for lo, hi in got_boxes:
            c = (lo + hi) / 2
            self.assertLess(np.abs(c[:2] - want_c[:2]).max(), TOL)

    # ------------------------------------------------------------ Aus- und Einblenden

    def test_hide_show_and_hidden_layer(self):
        ops = [
            {"op": "duplicate", "select": {"name": "Chair"}, "offset": [0.7, 0, 0]},
            {"op": "hide", "select": {"name": "Chair.001"}},           # nur die Kopie
            {"op": "hide", "select": {"name": "Leg_Table.002"}},       # ein einzelnes Bein
            {"op": "hide", "select": {"name": "Tabletop"}},
            {"op": "show", "select": {"name": "Tabletop"}},            # wieder da
            {"op": "set_layer", "select": {"name": "Table"}, "layer": "Table"},
            {"op": "hide_layer", "layer": "Table"},                    # Ebene, nicht Objekt
        ]
        m, text = self.edit(ops, "verborgen.skp")
        self.assertIn("hidden=9", text)  # Stuhl mit Inhalt: Rahmen, Lehne (2 + Huelle), 4 Beine, Sitz
        pl = placements(m)
        self.assertEqual(core.placed_face_count(m), 128 + 70)
        chairs = roots_named(pl, "Chair")
        self.assertEqual(len(chairs), 2)
        by_x = sorted(chairs, key=lambda i: pl[i]["t"][0])
        self.assertEqual([pl[i]["inst"].hidden for i in by_x], [False, True])  # nur die Kopie
        # nur die aeussere Gruppe traegt das Merkmal: beide Stuehle teilen die Definition
        self.assertEqual(pl[by_x[0]]["inst"].ref_idx, pl[by_x[1]]["inst"].ref_idx)
        self.assertFalse(any(p["inst"].hidden for p in pl if p["depth"] > 0 and p["root"] in chairs))
        (table,) = roots_named(pl, "Table")
        self.assertFalse(pl[table]["inst"].hidden)      # die Ebene ist aus, nicht das Objekt
        self.assertEqual(pl[table]["inst"].layer, "Table")
        self.assertEqual({l.name: l.hidden for l in m.layers}.get("Table"), True)
        self.assertEqual({l.name: l.hidden for l in m.layers}.get("Chair"), False)
        legs = [p for p in pl if p["defn"].name == "Leg_Table"]
        hidden = [(round(p["inst"].matrix[9], 1), round(p["inst"].matrix[10], 1)) for p in legs if p["inst"].hidden]
        self.assertEqual(hidden, [(10.8, -8.8)])        # Leg_Table.002, die anderen sichtbar
        top = [p for p in pl if p["defn"].name == "Tabletop"]
        self.assertEqual([p["inst"].hidden for p in top], [False])

    # ------------------------------------------------------------ Spiegeln

    def test_mirror_keeps_faces_and_orientation(self):
        ops = [{"op": "mirror", "select": {"name": "*"}, "axis": "x", "pivot": "origin"}]
        m, text = self.edit(ops, "gespiegelt.skp")
        self.assertIn("mirrored=2", text)
        self.assertEqual(core.placed_face_count(m), 128)
        self.assertEqual(sum(len(d.faces) for d in m.definitions.values()),
                         sum(len(d.faces) for d in self.orig.definitions.values()))
        pl = placements(m)
        for i in roots_named(pl, "Chair") + roots_named(pl, "Table"):
            self.assertLess(np.linalg.det(pl[i]["M"]), 0)  # Spiegelung an der Platzierung
        before, after = all_faces(self.orig_pl), all_faces(pl)
        (lo0, hi0), (lo1, hi1) = box(before), box(after)
        self.assertAlmostEqual(lo1[0], -hi0[0], delta=TOL)  # Box in x umgeklappt
        self.assertAlmostEqual(hi1[0], -lo0[0], delta=TOL)
        self.assertLess(np.abs(lo1[1:] - lo0[1:]).max(), TOL)
        self.assertLess(np.abs(hi1[1:] - hi0[1:]).max(), TOL)
        # jede Flaeche liegt gespiegelt da und zeigt mit der Vorderseite weiter nach aussen:
        # erwartet ist das Spiegelbild von Punkten UND Normale, nicht eine umgedrehte Flaeche
        R = np.diag([-1.0, 1.0, 1.0])
        match_faces(self, [(pts @ R, R @ n) for pts, n in before], after)

    # ------------------------------------------------------------ Nur lesen

    def test_measure_is_read_only(self):
        ops = [{"op": "measure", "select": {"name": "Chair"}, "to_object": {"name": "Table"}},
               {"op": "measure"}]
        m, text = self.edit(ops, "gemessen.skp")
        self.assertIn("distance=", text)
        self.assertIn("size=[1.2446, 0.508, 0.8636]", text)  # ganzes Modell
        pl = placements(m)
        self.assertEqual(core.placed_face_count(m), 128)
        match_faces(self, all_faces(self.orig_pl), all_faces(pl))  # nichts bewegt

    # ------------------------------------------------------------ Fehler schreiben nichts

    def test_invalid_input_writes_nothing(self):
        out = self.tmp / "nie.skp"
        bad = [
            '[{"op": "array", "select": {"name": "Chair"}, "counts": [2, 1, 1], "spacing": [NaN, 0, 0]}]',
            json.dumps([{"op": "array", "select": {"name": "Chair"}, "counts": [2, 1, 1],
                         "spacing": [1e999, 0, 0]}]),  # json liest 1e999 als unendlich
            json.dumps([{"op": "array", "select": {"name": "Chair"}, "counts": [-2, 1, 1], "spacing": 1}]),
            json.dumps([{"op": "hide", "select": {"name": "Chair\x1b[2J"}}]),
            json.dumps([{"op": "distribute", "select": {"name": "*"}, "axis": "x", "gap": True}]),
            json.dumps([{"op": "array", "select": {"name": "*", "type": "any"}, "counts": [100, 100, 1],
                         "spacing": [1, 1, 0]}]),
        ]
        for ops in bad:
            code, text, err = run_cli("edit", str(S2017), "-o", str(out), "--ops", ops, "-q")
            self.assertNotEqual(code, 0, ops)
            self.assertFalse(out.exists(), ops)
            self.assertNotIn("Traceback", err + text)


@unittest.skipUnless(BLENDER, "Blender nicht installiert")
class TestOpsDirekt(unittest.TestCase):
    """ops.run direkt in Blender: Meldungen je Fehlerfall, Budget vor dem Anlegen, measure."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="skptool_opsdirekt_"))
        cls.blend = cls.tmp / "stuhl.blend"
        code, text, err = run_cli("convert", str(S2017), "-o", str(cls.blend), "-q")
        assert code == 0, text + err
        cls.orig_pl = placements(load(S2017))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_cases(self, cases):
        script, cases_path, out = self.tmp / "harness.py", self.tmp / "faelle.json", self.tmp / "ergebnis.json"
        script.write_text(HARNESS, encoding="utf-8")
        # allow_nan: NaN und Infinity sollen ops.py wirklich erreichen (skptool edit filtert NaN vorher)
        cases_path.write_text(json.dumps(cases, allow_nan=True), encoding="utf-8")
        subprocess.run([BLENDER, "-b", "--factory-startup", "--python", str(script), "--",
                        str(self.blend), str(SCRIPTS), str(cases_path), str(out)],
                       check=True, capture_output=True, timeout=300)
        return json.loads(out.read_text(encoding="utf-8"))

    def test_bad_inputs_are_rejected_with_german_messages(self):
        chair = {"name": "Chair"}
        nan, inf = float("nan"), float("inf")
        cases = [
            ({"op": "align", "select": chair, "axis": "w"}, "axis muss x, y oder z sein"),
            ({"op": "align", "select": chair, "axis": ["x", "x"]}, "Achse doppelt"),
            ({"op": "align", "select": chair, "axis": "x", "to": "mitte"}, 'to muss "min", "center" oder "max"'),
            ({"op": "align", "select": chair, "axis": "x", "axsi": "y"}, "Unbekannte Felder fuer align"),
            ({"op": "align", "select": chair, "axis": "x", "to_object": {"name": "Gibts*"}}, "Keine Objekte passen"),
            ({"op": "align", "select": chair, "axis": "x", "to_object": chair}, "Nichts auszurichten"),
            ({"op": "align", "select": chair, "axis": "x", "to_object": {"name": "Seat_Chair"}},
             "liegt in einem der auszurichtenden"),
            ({"op": "align", "select": chair}, "align braucht"),
            ({"op": "distribute", "select": {"name": "*"}, "axis": "x"}, "mindestens 3 Objekte"),
            ({"op": "distribute", "select": {"name": "*"}, "axis": "x", "gap": True}, "gap muss eine Zahl sein"),
            ({"op": "distribute", "select": {"name": "*"}, "axis": "x", "gap": nan}, "gap muss zwischen"),
            ({"op": "distribute", "select": {"name": "*"}, "axis": "x", "gap": 1e7}, "gap muss zwischen"),
            ({"op": "array", "select": chair, "counts": [-1, 2, 1], "spacing": 1}, "counts muss eine ganze Zahl"),
            ({"op": "array", "select": chair, "counts": [1.5, 2, 1], "spacing": 1}, "counts muss eine ganze Zahl"),
            ({"op": "array", "select": chair, "counts": [True, 2, 1], "spacing": 1}, "counts muss eine ganze Zahl"),
            ({"op": "array", "select": chair, "counts": [1001, 1, 1], "spacing": 1}, "counts muss eine ganze Zahl"),
            ({"op": "array", "select": chair, "counts": [1, 1, 1], "spacing": 1}, "keine Kopie"),
            ({"op": "array", "select": chair, "counts": [2, 1], "spacing": 1}, "counts braucht [nx, ny, nz]"),
            ({"op": "array", "select": chair, "counts": [2, 1, 1]}, "array braucht"),
            ({"op": "array", "select": chair, "counts": [2, 1, 1], "spacing": "nan"}, "spacing braucht eine Zahl"),
            ({"op": "array", "select": chair, "counts": [2, 1, 1], "spacing": [inf, 0, 0]}, "spacing muss zwischen"),
            ({"op": "array", "select": chair, "counts": [1000, 1, 1], "spacing": [1e6, 0, 0]}, "weiter als"),
            ({"op": "array", "select": chair, "counts": [2, 1, 1], "spacing": 1, "offset": 1}, "Unbekannte Felder"),
            ({"op": "mirror", "select": chair, "axis": ["x"]}, "axis muss x, y oder z sein"),
            ({"op": "mirror", "select": chair, "axis": "x", "pivot": "bottom"}, "pivot muss"),
            ({"op": "mirror", "select": chair, "axis": "x", "pivot": [0, nan, 0]}, "pivot muss zwischen"),
            ({"op": "hide", "select": {"name": "Chair\x07"}}, "enthaelt Steuerzeichen"),
            ({"op": "hide", "select": {"name": "x" * 300}}, "zu lang"),
            ({"op": "show", "select": chair, "layer": "Chair"}, "Unbekannte Felder fuer show"),
            ({"op": "measure", "select": chair, "to_object": "Table"}, "select muss ein Objekt sein"),
            ({"op": "measure", "select": {"name": "Gibts*"}}, "Keine Objekte passen"),
        ]
        results = self.run_cases([[op] for op, _ in cases])
        for (op, want), res in zip(cases, results):
            r = res["results"][0]
            self.assertFalse(r["ok"], op)
            self.assertIn(want, r["error"], op)
            self.assertEqual(res["objects_after"], res["objects_before"], op)

    def test_array_checks_budget_before_creating(self):
        big = {"op": "array", "select": {"name": "*", "type": "any"}, "counts": [100, 100, 1], "spacing": [1, 1, 0]}
        res = self.run_cases([[big], [{"op": "summary"}]])
        self.assertFalse(res[0]["results"][0]["ok"])
        self.assertIn("hoechstens 100000", res[0]["results"][0]["error"])
        self.assertEqual(res[0]["objects_after"], res[0]["objects_before"])  # nichts angelegt
        self.assertEqual(res[1]["results"][0]["objects"], 14)

    def test_measure_hide_show_and_mirror_self(self):
        cases = [
            [{"op": "measure", "select": {"name": "Chair"}, "to_object": {"name": "Table"}}],
            [{"op": "measure"}],
            [{"op": "hide", "select": {"name": "Chair"}}, {"op": "list", "select": {"name": "Leg_Chair*"}}],
            [{"op": "show"}, {"op": "list", "select": {"name": "*", "type": "any"}}],
            [{"op": "measure", "select": {"name": "Chair"}},
             {"op": "measure", "select": {"name": "BackrestFrame"}},
             {"op": "mirror", "select": {"name": "Chair"}, "axis": "y"},
             {"op": "measure", "select": {"name": "Chair"}},
             {"op": "measure", "select": {"name": "BackrestFrame"}}],
        ]
        res = self.run_cases(cases)
        # measure: Box samt Inhalt wie in der .skp, Abstand der Mitten
        (chair,), (table,) = roots_named(self.orig_pl, "Chair"), roots_named(self.orig_pl, "Table")
        clo, chi = box(all_faces(self.orig_pl, chair))
        tlo, thi = box(all_faces(self.orig_pl, table))
        r = res[0]["results"][0]
        self.assertTrue(r["ok"], r)
        self.assertLess(np.abs(np.array(r["size"]) - (chi - clo)).max(), TOL)
        self.assertLess(np.abs(np.array(r["center"]) - (clo + chi) / 2).max(), TOL)
        self.assertLess(np.abs(np.array(r["other"]["min"]) - tlo).max(), TOL)
        dist = np.linalg.norm((tlo + thi) / 2 - (clo + chi) / 2)
        self.assertAlmostEqual(r["distance"], dist, delta=TOL)
        self.assertLess(np.abs(np.array(r["size"]) - np.array([0.4572, 0.4318, 0.8636])).max(), TOL)
        whole = res[1]["results"][0]
        self.assertLess(np.abs(np.array(whole["size"]) - np.array([1.2446, 0.508, 0.8636])).max(), TOL)
        # hide: Stuhl samt Inhalt; show ohne Auswahl: wieder alles sichtbar
        self.assertEqual(res[2]["results"][0]["hidden"], 9)
        self.assertTrue(all(o["hidden"] for o in res[2]["results"][1]["objects"]))
        self.assertFalse(any(o["hidden"] for o in res[3]["results"][1]["objects"]))
        # mirror um die eigene Mitte: Box bleibt, die Lehne wandert auf die andere Seite
        c0, back0, _m, c1, back1 = res[4]["results"]
        self.assertEqual(c0["size"], c1["size"])
        self.assertEqual(c0["center"], c1["center"])
        self.assertAlmostEqual(back1["center"][1] - c0["center"][1], -(back0["center"][1] - c0["center"][1]),
                               delta=TOL)
        self.assertGreater(abs(back0["center"][1] - c0["center"][1]), 0.1)


if __name__ == "__main__":
    unittest.main()
