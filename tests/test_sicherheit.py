"""Sicherheitstests: jeder Fund aus dem Audit vor der Veroeffentlichung hat hier einen Test.

Start: .venv\\Scripts\\python -m unittest tests.test_sicherheit -v
Tests mit Blender werden uebersprungen, wenn Blender fehlt.
"""
import base64
import importlib.util
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from skptool import blender as bl
from skptool import cli, core
from skptool.opsjson import load_ops

ROOT = Path(__file__).resolve().parents[1]
S2017 = ROOT / "samples" / "stuhl_tisch_2017.skp"
UNC = "\\\\evil-host.invalid\\share\\x.png"
LAUNCHER = Path(os.environ.get("SKPTOOL_TEST_LAUNCHER", ROOT / "skptool.cmd"))  # zum Gegenpruefen austauschbar

try:
    BLENDER = bl.find_blender()
except bl.BlenderError:
    BLENDER = None

_spec = importlib.util.spec_from_file_location("refcheck", ROOT / "skptool" / "blender_scripts" / "refcheck.py")
refcheck = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(refcheck)


def run_cli(*args):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with redirect_stdout(out), redirect_stderr(err):
        try:
            cli.main(list(args))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
            if not isinstance(exc.code, int) and exc.code:
                err.write(str(exc.code))
    return code, out.getvalue(), err.getvalue()


def gltf_with(image_uri, buffer_uri=None):
    buf = struct.pack("<9f", 0, 0, 0, 1, 0, 0, 0, 1, 0)
    buffer_uri = buffer_uri or "data:application/octet-stream;base64," + base64.b64encode(buf).decode()
    return {
        "asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "material": 0}]}],
        "materials": [{"pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}}],
        "textures": [{"source": 0}], "images": [{"uri": image_uri}],
        "buffers": [{"byteLength": len(buf), "uri": buffer_uri}],
        "bufferViews": [{"buffer": 0, "byteLength": 36}],
        "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3",
                       "min": [0, 0, 0], "max": [1, 1, 0]}],
    }


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="skptool_sicher_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------- Verweise in fremden Dateien

class TestReferenceCheck(Tmp):
    def test_network_paths_are_recognised_in_every_spelling(self):
        for p in (UNC, "//evil/share/x.png", "file://evil/share/x.png", "%5C%5Cevil%5Cs%5Cx.png",
                  "\\\\?\\UNC\\evil\\share\\x.png", "smb://evil/x", "https://evil.example/x.png",
                  "\\\\evil@SSL\\DavWWWRoot\\x.png"):
            self.assertEqual(refcheck.classify(p), "network", p)
        for p in ("tex/a.png", "../a.png", "C:\\Users\\x\\a.png", "C:/x/a.png", "\\\\?\\C:\\x\\a.png",
                  "file:///C:/x/a.png"):
            self.assertEqual(refcheck.classify(p), "local", p)
        self.assertEqual(refcheck.classify("data:image/png;base64,AAAA"), "data")

    def test_gltf_network_uri_is_refused_even_when_allowed(self):
        for uri in (UNC, "%5C%5Cevil%5Cshare%5Cx.png", "//evil/share/x.png", "file://evil/s/x.png"):
            f = self.tmp / "m.gltf"
            f.write_text(json.dumps(gltf_with(uri)))
            for allow in (False, True):
                with self.assertRaises(refcheck.RefError) as cm:
                    refcheck.precheck(str(f), allow)
                self.assertIn("Netzwerkpfade", str(cm.exception))

    def test_gltf_file_outside_is_refused_unless_allowed(self):
        f = self.tmp / "modell" / "m.gltf"
        f.parent.mkdir()
        f.write_text(json.dumps(gltf_with("../privat/geheim.png")))
        with self.assertRaises(refcheck.RefError) as cm:
            refcheck.precheck(str(f), False)
        self.assertIn("--allow-external", str(cm.exception))
        refcheck.precheck(str(f), True)  # ausdruecklich erlaubt
        # externer Puffer (Geometrie aus einer beliebigen lokalen Datei) genauso
        f.write_text(json.dumps(gltf_with("data:image/png;base64,AAAA", "../../geheim.bin")))
        with self.assertRaises(refcheck.RefError):
            refcheck.precheck(str(f), False)

    def test_glb_json_chunk_is_checked(self):
        js = json.dumps(gltf_with(UNC)).encode()
        js += b" " * (-len(js) % 4)
        glb = struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(js)) + struct.pack("<II", len(js), 0x4E4F534A) + js
        f = self.tmp / "m.glb"
        f.write_bytes(glb)
        with self.assertRaises(refcheck.RefError):
            refcheck.precheck(str(f), True)

    def test_obj_mtl_with_network_texture_is_refused(self):
        (self.tmp / "m.obj").write_text("mtllib m.mtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n")
        (self.tmp / "m.mtl").write_text(f"newmtl a\nmap_Kd -s 1 1 1 {UNC}\n")
        with self.assertRaises(refcheck.RefError):
            refcheck.precheck(str(self.tmp / "m.obj"), True)
        (self.tmp / "n.obj").write_text(f"mtllib {UNC}\nv 0 0 0\n")
        with self.assertRaises(refcheck.RefError):
            refcheck.precheck(str(self.tmp / "n.obj"), True)
        (self.tmp / "o.obj").write_text("mtllib ../../fremd.mtl\nv 0 0 0\n")
        with self.assertRaises(refcheck.RefError):
            refcheck.precheck(str(self.tmp / "o.obj"), False)

    def test_fbx_and_uncompressed_blend_network_strings(self):
        f = self.tmp / "m.fbx"
        f.write_bytes(b"Kaydara FBX Binary  \x00" + b"\x00" * 50 + b"S\x20\x00\x00\x00" + UNC.encode() + b"\x00" * 20)
        with self.assertRaises(refcheck.RefError):
            refcheck.precheck(str(f), True)
        b = self.tmp / "m.blend"
        b.write_bytes(b"BLENDER-v405" + b"IM\x00\x00" + struct.pack("<iQii", 64, 1, 0, 1)
                      + b"\x00" * 8 + UNC.encode() + b"\x00" * 30 + b"ENDB" + b"\x00" * 20)
        with self.assertRaises(refcheck.RefError):
            refcheck.precheck(str(b), True)

    def test_unreadable_files_are_refused_not_opened(self):
        b = self.tmp / "kaputt.blend"
        b.write_bytes(b"BLENDERxx garbage")
        with self.assertRaises(refcheck.RefError):
            refcheck.precheck(str(b), True)

    def test_usdz_zip_bomb_is_refused(self):
        import zipfile
        f = self.tmp / "b.usdz"
        with zipfile.ZipFile(f, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("a.usdc", bytes(30 * 2**20))
        with self.assertRaises(refcheck.RefError):
            refcheck.check_zip(str(f))


# ---------------------------------------------------------------- Programmstart und Ausgabe

class TestStartup(Tmp):
    def test_blender_is_never_taken_from_the_current_folder(self):
        fake = self.tmp / ("blender.exe" if os.name == "nt" else "blender")
        fake.write_bytes(b"MZ nicht ausfuehren")
        fake.chmod(0o755)
        old = os.getcwd()
        os.chdir(self.tmp)
        try:
            with mock.patch.dict(os.environ, {"PATH": os.pathsep.join([".", "", str(self.tmp)])}):
                self.assertIsNone(bl._on_path("blender"))
        finally:
            os.chdir(old)

    @unittest.skipUnless(os.name == "nt", "skptool.cmd gibt es nur unter Windows")
    @unittest.skipUnless((LAUNCHER.parent / ".venv").exists() or "SKPTOOL_TEST_LAUNCHER" in os.environ,
                         "skptool.cmd braucht die .venv im Projektordner")
    def test_launcher_never_imports_from_the_current_folder(self):
        for mod in ("glob", "json", "numpy", "skptool"):
            (self.tmp / f"{mod}.py").write_text(f"open(r'{self.tmp}\\\\PWNED_{mod}', 'w')\n")
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "NoDefaultCurrentDirectoryInExePath")}
        r = subprocess.run(["cmd", "/c", str(LAUNCHER), "--version"], cwd=self.tmp, env=env,
                           capture_output=True, timeout=120, text=True)
        self.assertEqual(sorted(p.name for p in self.tmp.glob("PWNED_*")), [])
        self.assertIn("skptool", r.stdout)  # der Starter lief wirklich

    def test_names_from_files_cannot_control_the_terminal(self):
        b = core.create()
        b.add_layer("\x1b]52;c;Y2FsYy5leGU=\x07Ebene\u202e")
        b.add_face([(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0)])
        f = self.tmp / "boese.skp"
        core.save_atomic(b, f)
        code, out, err = run_cli("info", str(f))
        self.assertEqual(code, 0, err)
        self.assertIn("Ebene", out)
        for bad in ("\x1b", "\x07", "\u202e"):
            self.assertNotIn(bad, out + err)
        self.assertIn("\\x1b", out)

    def test_ops_json_is_strict(self):
        for raw in ('[{"op": "move", "by": [NaN, 0, 0]}]', '[{"op": "move", "by": [Infinity, 0, 0]}]',
                    "nicht_da.json", '[{"op": 1}]', "[" * 50000, json.dumps([{"op": "summary"}] * 1001)):
            with self.assertRaises(SystemExit, msg=raw[:40]):
                load_ops(raw)
        self.assertEqual(load_ops('{"op": "summary"}'), [{"op": "summary"}])


# ---------------------------------------------------------------- Dateien nie ueberschreiben

class TestOverwrite(Tmp):
    def test_same_file_through_long_path_is_refused(self):
        src = self.tmp / "a.skp"
        shutil.copy(S2017, src)
        before = src.read_bytes()
        target = "\\\\?\\" + str(src.resolve()) if os.name == "nt" else str(self.tmp / "." / "a.skp")
        code, _, err = run_cli("convert", str(src), "-o", target, "-q")
        self.assertNotEqual(code, 0)
        self.assertEqual(src.read_bytes(), before)

    def test_batch_refuses_existing_duplicate_and_input_targets(self):
        for d in ("d1", "d2", "out"):
            (self.tmp / d).mkdir()
        shutil.copy(S2017, self.tmp / "d1" / "x.skp")
        shutil.copy(S2017, self.tmp / "d2" / "x.skp")
        code, _, err = run_cli("convert", str(self.tmp / "d1" / "x.skp"), str(self.tmp / "d2" / "x.skp"),
                               "-f", "glb", "-d", str(self.tmp / "out"), "-q")
        self.assertNotEqual(code, 0)
        self.assertIn("beide", err)
        self.assertEqual(list((self.tmp / "out").iterdir()), [])
        precious = self.tmp / "out" / "x.glb"
        precious.write_bytes(b"wertvoll")
        shutil.copy(S2017, self.tmp / "d1" / "y.skp")
        code, _, err = run_cli("convert", str(self.tmp / "d1" / "*.skp"), "-f", "glb", "-d",
                               str(self.tmp / "out"), "-q")
        self.assertNotEqual(code, 0)
        self.assertEqual(precious.read_bytes(), b"wertvoll")
        code, _, err = run_cli("convert", str(self.tmp / "d1" / "*.skp"), "-f", "glb", "-d",
                               str(self.tmp / "out"), "-q", "--force")
        self.assertEqual(code, 0, err)


# ---------------------------------------------------------------- mit Blender

EVIL_BLENDS = r"""
import bpy, os, sys
out, priv = sys.argv[-2], sys.argv[-1]
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add()
im = bpy.data.images.new("netz", 4, 4)
im.source = "FILE"
im.filepath = "\\\\evil-host.invalid\\share\\x.png"
im.use_fake_user = True
bpy.ops.wm.save_as_mainfile(filepath=os.path.join(out, "netzbild.blend"), compress=True)
bpy.ops.wm.read_factory_settings(use_empty=True)
with open(os.path.join(priv, "geheim.obj"), "w") as fh:
    fh.write("v 0 0 0\nv 50 0 0\nv 0 50 0\nf 1 2 3\n")
bpy.ops.mesh.primitive_cube_add()
ob = bpy.context.object
ng = bpy.data.node_groups.new("Leser", "GeometryNodeTree")
ng.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
imp = ng.nodes.new("GeometryNodeImportOBJ")
imp.inputs["Path"].default_value = os.path.join(priv, "geheim.obj")
gi, go = ng.nodes.new("NodeGroupInput"), ng.nodes.new("NodeGroupOutput")
join = ng.nodes.new("GeometryNodeJoinGeometry")
ng.links.new(gi.outputs[0], join.inputs[0])
ng.links.new(imp.outputs[0], join.inputs[0])
ng.links.new(join.outputs[0], go.inputs[0])
mod = ob.modifiers.new("Leser", "NODES")
mod.node_group = ng
cache = ob.modifiers.new("Cache", "MESH_CACHE")
cache.filepath = os.path.join(priv, "geheim.mdd")
bpy.ops.wm.save_as_mainfile(filepath=os.path.join(out, "leser.blend"), compress=True)
"""


@unittest.skipUnless(BLENDER, "Blender nicht installiert")
class TestWithBlender(Tmp):
    def _make(self):
        (self.tmp / "modell").mkdir()
        (self.tmp / "privat").mkdir()
        script = self.tmp / "mk.py"
        script.write_text(EVIL_BLENDS, encoding="utf-8")
        subprocess.run([BLENDER, "-b", "--factory-startup", "-Y", "--python", str(script), "--",
                        str(self.tmp / "modell"), str(self.tmp / "privat")], check=True, capture_output=True,
                       timeout=300)

    def _need_unc_in_fixture(self, name):
        """Blender ausserhalb von Windows speichert einen Netzwerkpfad mit Backslashes als "//host/x",
        also als Pfad relativ zur Datei: dann steckt in der erzeugten Testdatei gar kein Netzwerkpfad
        (und sie ist dort zu Recht harmlos). Handgebaute Dateien mit echten Backslashes prueft
        TestReferenceCheck.test_fbx_and_uncompressed_blend_network_strings auf jedem System."""
        if os.name != "nt":
            self.skipTest("Blender speichert Backslash-Pfade hier als relative Pfade (//...)")

    def test_blend_with_network_image_is_refused_before_opening(self):
        self._make()
        self._need_unc_in_fixture("netzbild.blend")
        code, _, err = run_cli("convert", str(self.tmp / "modell" / "netzbild.blend"),
                               "-o", str(self.tmp / "x.skp"), "-q", "--allow-external")
        self.assertNotEqual(code, 0)
        self.assertIn("Netzwerkpfade", err)
        self.assertFalse((self.tmp / "x.skp").exists())

    def test_nodes_and_caches_that_read_files_are_removed(self):
        self._make()
        out = self.tmp / "x.skp"
        code, text, err = run_cli("convert", str(self.tmp / "modell" / "leser.blend"), "-o", str(out))
        self.assertEqual(code, 0, err)
        self.assertIn("Import OBJ", err)
        self.assertIn("Cache", err)
        m = core.model_of(core.open_skp(out))
        self.assertEqual(core.placed_face_count(m), 6)  # nur der Wuerfel, nicht das geheime Dreieck

    def test_open_checks_a_blend_before_the_window_loads_it(self):
        self._make()
        self._need_unc_in_fixture("netzbild.blend")
        clean = self.tmp / "sauber.blend"
        self.assertEqual(run_cli("convert", str(S2017), "-o", str(clean), "-q")[0], 0)
        with mock.patch.object(cli, "launch_gui") as gui:
            code, _, err = run_cli("open", str(self.tmp / "modell" / "netzbild.blend"), "--allow-external")
            self.assertNotEqual(code, 0)
            self.assertIn("Netzwerkpfade", err)
            code, _, err = run_cli("open", str(self.tmp / "modell" / "leser.blend"))
            self.assertNotEqual(code, 0)
            self.assertIn("--allow-external", err)
            gui.assert_not_called()
            code, _, err = run_cli("open", str(self.tmp / "modell" / "leser.blend"), "--allow-external")
            self.assertEqual(code, 0, err)
            code, _, err = run_cli("open", str(clean))
            self.assertEqual(code, 0, err)
            self.assertEqual(gui.call_count, 2)
            self.assertEqual(Path(gui.call_args[0][0]), clean.resolve())

    def test_edit_rejects_bad_numbers_names_and_too_much_work(self):
        out = self.tmp / "x.skp"
        bad = [
            [{"op": "move", "select": {"name": "Table"}, "by": [1e39, 0, 0]}],
            [{"op": "scale", "select": {"name": "Table"}, "factor": True}],
            [{"op": "rotate", "select": {"name": "Table"}, "deg": "nan"}],
            [{"op": "rename", "select": {"name": "Table"}, "to": "\x1b[31mRot"}],
            [{"op": "set_layer", "select": {"name": "Table"}, "layer": "x" * 64}],
            [{"op": "move", "select": "Table", "by": [1, 0, 0]}],
            [{"op": "duplicate", "select": {"name": "*"}, "count": 10},
             {"op": "duplicate", "select": {"name": "*", "type": "any"}, "count": 1000}],
        ]
        for ops in bad:
            code, _, err = run_cli("edit", str(S2017), "-o", str(out), "--ops", json.dumps(ops), "-q")
            self.assertNotEqual(code, 0, ops)
            self.assertFalse(out.exists(), ops)
            self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
