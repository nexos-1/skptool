"""Fuzz-Regressionen: feindliche Eingaben duerfen nicht abstuerzen, haengen oder Speicher sprengen.

Start: .venv\\Scripts\\python -m unittest tests.test_fuzz -v

Enthaelt kleine deterministische Ausloeser aus der Fuzz-Kampagne (tools/fuzz.py) und eine kurze,
mit festem Startwert wiederholbare Rauch-Kampagne ueber die schnellen In-Prozess-Ziele. Die
langsamen Ziele (CLI-Subprozesse, MCP, Blender) laufen in tools/fuzz.py, nicht in der Suite.
"""
import importlib.util
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from skptool import core
from skptool.opsjson import load_ops

ROOT = Path(__file__).resolve().parents[1]


def _load_module(name, pfad):
    spec = importlib.util.spec_from_file_location(name, pfad)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


refcheck = _load_module("refcheck", ROOT / "skptool" / "blender_scripts" / "refcheck.py")
fuzz = _load_module("skptool_fuzz", ROOT / "tools" / "fuzz.py")


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="skptool_fuzz_"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------- header_version liest nie die ganze Datei

class ByteZaehler:
    """Datei-Wrapper, der zaehlt, wie viele Bytes gelesen werden, und bei zu viel sofort meckert."""

    def __init__(self, inner, grenze):
        self._inner = inner
        self.grenze = grenze
        self.gelesen = 0

    def read(self, n=-1):
        if n is None or n < 0 or n > self.grenze:
            raise AssertionError(f"header_version liest zu viel auf einmal: read({n})")
        data = self._inner.read(n)
        self.gelesen += len(data)
        return data

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, *a):
        return self._inner.__exit__(*a)


class TestHeaderVersionBounded(Tmp):
    """Regression: header_version() las frueher die ganze Datei ein (read_bytes()[:200]), eine
    praeparierte Riesendatei sprengte so beim blossen 'info' den Speicher. Jetzt nur der Kopf."""

    def _make(self, tail_mb):
        p = self.tmp / "gross.skp"
        head = b"\xff\xfe\xff\x0e" + "SketchUp Model{17.0.1}".encode("utf-16-le")
        with open(p, "wb") as fh:
            fh.write(head)
            chunk = b"\x00" * (1024 * 1024)
            for _ in range(tail_mb):
                fh.write(chunk)
        return p

    def test_reads_only_the_header(self):
        p = self._make(8)  # 8 MB Rumpf, echte Datei
        real_open = open
        zaehler = {}

        def fake_open(path, mode="r", *a, **kw):
            fh = real_open(path, mode, *a, **kw)
            if str(path) == str(p) and "b" in mode:
                w = ByteZaehler(fh, 4096)
                zaehler["w"] = w
                return w
            return fh

        import builtins
        orig = builtins.open
        builtins.open = fake_open
        try:
            v = core.header_version(p)
        finally:
            builtins.open = orig
        self.assertEqual(v, "17.0.1")
        self.assertIn("w", zaehler, "header_version hat die Datei nicht ueber open() gelesen")
        self.assertLessEqual(zaehler["w"].gelesen, 4096,
                             "header_version liest mehr als den Kopf (ganze Datei?)")

    def test_still_reports_version_and_rejects_non_skp(self):
        self.assertEqual(core.header_version(self._make(1)), "17.0.1")
        other = self.tmp / "x.bin"
        other.write_bytes(b"not a sketchup file" * 100)
        with self.assertRaises(ValueError):
            core.header_version(other)


# ---------------------------------------------------------------- opsjson: nur SystemExit, nie Traceback

class TestOpsFuzz(unittest.TestCase):
    def test_hostile_ops_only_systemexit(self):
        import numpy as np
        rng = np.random.default_rng(42)
        for _ in range(400):
            data = fuzz.hostile_ops(rng)
            raw = data.decode("latin-1")
            try:
                load_ops(raw)
            except SystemExit:
                pass
            except Exception as exc:  # noqa: BLE001
                self.fail(f"load_ops warf {type(exc).__name__} statt SystemExit: {exc!r} fuer {data[:80]!r}")

    def test_known_reproducers(self):
        # tiefe Verschachtelung (RecursionError -> saubere Meldung)
        for raw in ("[" * 5000, "[" + "0," * 50000 + "0]", "NaN", "[{\"op\": 1}]",
                    "[{\"op\": \"x\", \"f\": 1e400}]"):
            with self.assertRaises(SystemExit):
                load_ops(raw)


# ---------------------------------------------------------------- refcheck: nur RefError

class TestRefcheckFuzz(Tmp):
    def test_hostile_files_only_referror(self):
        import numpy as np
        rng = np.random.default_rng(7)
        exts = list(fuzz.HOSTILE)
        for i in range(160):
            ext = exts[i % len(exts)]
            data = fuzz.HOSTILE[ext](rng)
            f = self.tmp / f"m{ext}"
            f.write_bytes(data)
            for allow in (False, True):
                try:
                    refcheck.precheck(str(f), allow)
                except refcheck.RefError:
                    pass
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"precheck({ext}, allow={allow}) warf {type(exc).__name__}: {exc!r}")

    def test_network_paths_stay_refused(self):
        # ein glTF mit UNC-Verweis wird immer abgelehnt, auch mit allow_external
        doc = {"asset": {"version": "2.0"}, "images": [{"uri": "\\\\evil\\share\\x.png"}]}
        f = self.tmp / "m.gltf"
        f.write_text(json.dumps(doc), encoding="utf-8")
        for allow in (False, True):
            with self.assertRaises(refcheck.RefError):
                refcheck.precheck(str(f), allow)


# ---------------------------------------------------------------- Container: nur UnsafeFileError

class TestContainerFuzz(Tmp):
    def test_hostile_zip_only_unsafe_or_clean(self):
        import numpy as np
        rng = np.random.default_rng(3)
        for i in range(40):
            data = fuzz.hostile_zip(rng)
            f = self.tmp / f"z{i % 4}.skp"
            f.write_bytes(data)
            try:
                core.check_container(f)
            except ValueError as exc:  # UnsafeFileError ist ein ValueError; beide mit deutscher Meldung
                self.assertNotIn("Error", str(exc))
            except Exception as exc:  # noqa: BLE001
                self.fail(f"check_container warf {type(exc).__name__}: {exc!r}")
            try:
                core.header_version(f)
            except ValueError:
                pass
            except Exception as exc:  # noqa: BLE001
                self.fail(f"header_version warf {type(exc).__name__}: {exc!r}")

    def test_zip_bomb_is_refused(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("model.dat", b"\x00" * (9 * 2**30 // 100))  # weit ueber jeder ehrlichen Datei
        f = self.tmp / "bomb.skp"
        f.write_bytes(buf.getvalue())
        # Entweder Ratio- oder Groessengrenze greift; auf keinen Fall ein Traceback
        try:
            core.check_container(f)
        except core.UnsafeFileError:
            pass

    def test_unsupported_zip_version_is_clean(self):
        # Regression: ein verdrehtes Feld "benoetigte Version" im ZIP-Kopf liess zipfile
        # NotImplementedError werfen, das check_container frueher nicht abfing (Traceback statt
        # sauberer Meldung). Jetzt eine deutsche UnsafeFileError.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("model.dat", b"x")
        raw = bytearray(buf.getvalue())
        pos = raw.find(b"PK\x01\x02")  # zentrales Verzeichnis: "benoetigte Version" bei +6
        self.assertGreater(pos, 0)
        raw[pos + 6:pos + 8] = b"\xff\xff"
        f = self.tmp / "badver.skp"
        f.write_bytes(bytes(raw))
        with self.assertRaises(core.UnsafeFileError):
            core.check_container(f)

    def test_many_entries_refused(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for k in range(core.MAX_ZIP_ENTRIES + 5):
                z.writestr(f"e{k}", b"")
        f = self.tmp / "viele.skp"
        f.write_bytes(buf.getvalue())
        with self.assertRaises(core.UnsafeFileError):
            core.check_container(f)


# ---------------------------------------------------------------- Live-Rahmen: nur LiveError

class TestLiveFrameFuzz(unittest.TestCase):
    def test_read_line_only_liveerror(self):
        import numpy as np

        from skptool import live
        rng = np.random.default_rng(8)

        class Fake:
            def __init__(self, d):
                self.d = d
                self.i = 0

            def recv(self, n):
                if self.i >= len(self.d):
                    return b""
                chunk = self.d[self.i:self.i + int(rng.integers(1, 200))]
                self.i += len(chunk)
                return chunk

        for _ in range(300):
            data = bytes(rng.integers(0, 256, int(rng.integers(0, 5000)), dtype=np.uint8).tolist())
            try:
                live._read_line(Fake(data), 4096)
            except live.LiveError:
                pass
            except Exception as exc:  # noqa: BLE001
                self.fail(f"_read_line warf {type(exc).__name__}: {exc!r}")

    def test_oversized_frame_refused(self):
        from skptool import live

        class Flood:
            def recv(self, n):
                return b"x" * n  # nie ein Zeilenende: muss an der Grenze abbrechen

        with self.assertRaises(live.LiveError):
            live._read_line(Flood(), 4096)


class TestLiveClientDeadline(unittest.TestCase):
    """Regression: live._read_line hatte nur eine Frist je recv. Ein fremder Prozess auf dem Port, der
    alle paar Sekunden ein Byte ohne Zeilenende schickt, hielt 'skptool live' so bis zu einer halben
    Stunde fest (4096 Byte Begruessung). Jetzt gilt eine Gesamtfrist."""

    def test_read_line_total_deadline(self):
        import socket
        import threading
        import time

        from skptool import live
        a, b = socket.socketpair()
        stop = threading.Event()

        def tropfen():
            while not stop.is_set():
                try:
                    b.sendall(b"x")
                except OSError:
                    return
                stop.wait(0.1)

        threading.Thread(target=tropfen, daemon=True).start()
        t = time.monotonic()
        try:
            with self.assertRaises(socket.timeout):
                live._read_line(a, 4096, frist=0.8)
            self.assertLess(time.monotonic() - t, 3.0)
        finally:
            stop.set()
            a.close()
            b.close()

    def test_request_gives_up_against_dripping_server(self):
        from skptool import live
        dauer, fehler = fuzz.client_gegen_tropfserver(live, frist=25.0)
        self.assertEqual(fehler, "LiveError")
        self.assertLess(dauer, 20.0)


class TestLiveServerFraming(unittest.TestCase):
    """live_server.py in-Prozess (Ersatz-bpy): feindliche Rahmen, falsche HMAC, Wiederholung,
    uebergrosse Rahmen, Slowloris, zu viele Verbindungen. Danach muss ein gueltiges ping gehen."""

    def test_hostile_frames(self):
        import argparse
        a = argparse.Namespace(seed=3, cases=20, blender_cases=0, target="live-server",
                               outdir=str(Path(tempfile.mkdtemp(prefix="fuzz_live_"))))
        k = fuzz.Kampagne(a)
        k.fahre(["live-server"])
        self.assertEqual(k.funde, [], f"Live-Server: {k.funde}")


# ---------------------------------------------------------------- PLY mit riesigen angekuendigten Anzahlen

PLY_BOMBE = (b"ply\nformat ascii 1.0\nelement vertex 700000000\nproperty float x\nproperty float y\n"
             b"property float z\nend_header\n0 0 0\n")


class TestPlyDeclaredCounts(Tmp):
    """Regression (Blender-Kampagne): ein PLY-Kopf mit "element vertex 700000000" und 100 Byte Daten
    liess Blenders Importer 9 bis 12 GB reservieren. refcheck lehnt das jetzt vor dem Laden ab."""

    def test_bomb_is_refused(self):
        f = self.tmp / "b.ply"
        f.write_bytes(PLY_BOMBE)
        for allow in (False, True):
            with self.assertRaises(refcheck.RefError) as cm:
                refcheck.precheck(str(f), allow)
            self.assertIn("700000000 Elemente", str(cm.exception))

    def test_binary_count_beyond_file_is_refused(self):
        import struct
        head = (b"ply\nformat binary_little_endian 1.0\nelement vertex 4000000\nproperty float x\n"
                b"property float y\nproperty float z\nend_header\n")
        f = self.tmp / "b.ply"
        f.write_bytes(head + struct.pack("<3f", 0, 0, 0))
        with self.assertRaises(refcheck.RefError):
            refcheck.precheck(str(f))

    def test_valid_ply_still_passes(self):
        import struct
        a = self.tmp / "a.ply"
        a.write_text("ply\nformat ascii 1.0\nelement vertex 3\nproperty float x\nproperty float y\n"
                     "property float z\nelement face 1\nproperty list uchar int vertex_indices\nend_header\n"
                     "0 0 0\n1 0 0\n0 1 0\n3 0 1 2\n")
        refcheck.precheck(str(a))
        b = self.tmp / "b.ply"
        head = (b"ply\r\nformat binary_little_endian 1.0\r\nelement vertex 3\r\nproperty float x\r\n"
                b"property float y\r\nproperty float z\r\nelement face 1\r\n"
                b"property list uchar int vertex_indices\r\nend_header\r\n")
        b.write_bytes(head + struct.pack("<9f", 0, 0, 0, 1, 0, 0, 0, 1, 0) + struct.pack("<B3i", 3, 0, 1, 2))
        refcheck.precheck(str(b))
        # echte PLY aus dem OpenSKP-Export
        from skptool import core as _core
        c = self.tmp / "stuhl.ply"
        _core.export_native(_core.open_skp(ROOT / "samples" / "stuhl_tisch_2017.skp"), c)
        refcheck.precheck(str(c))

    def test_header_garbage_is_refused_cleanly(self):
        for data in (b"ply\nend_header\n", b"ply\nformat ascii 1.0\nelement vertex -5\nend_header\n",
                     b"ply\nformat ascii 1.0\nelement vertex x\nend_header\n", b"ply" + b"a" * 100, b""):
            f = self.tmp / "g.ply"
            f.write_bytes(data)
            with self.assertRaises(refcheck.RefError):
                refcheck.precheck(str(f))

    def test_blender_convert_refuses_without_memory_blowup(self):
        from skptool import blender
        try:
            blender.find_blender()
        except blender.BlenderError:
            self.skipTest("Blender fehlt")
        src = self.tmp / "b.ply"
        src.write_bytes(PLY_BOMBE)
        ziel = self.tmp / "aus"
        ziel.mkdir()
        befund = fuzz.lauf_prozess(["convert", str(src), "-o", str(ziel / "out.skp"), "-q"], 180, 4096, ziel)
        self.assertIsNone(befund["symptom"], befund)
        self.assertEqual(befund["rc"], 1)
        self.assertIsNone(fuzz.pruefe_meldung(befund["rc"], befund["log"]), befund["log"])
        self.assertIn("700000000 Elemente", befund["log"])
        self.assertFalse((ziel / "out.skp").exists())


# ---------------------------------------------------------------- eine lesbare deutsche Fehlerzeile

class TestOneReadableErrorLine(Tmp):
    def _cli(self, *args):
        import contextlib
        import io as _io

        from skptool import cli
        out, err = _io.StringIO(), _io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main(list(args))
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue() + err.getvalue()

    def test_container_without_model_dat(self):
        # Regression: OpenSKP meldete "KeyError: There is no item named 'model.dat' in the archive".
        # (Nicht in check_container abgefangen: auch Dateien vor 2021 lassen sich als ZIP oeffnen und
        # haben kein model.dat, etwa gondel_2020. Deshalb uebersetzt cli._explain den KeyError.)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("materials/a/material.xml", b"<x/>")
        f = self.tmp / "ohne.skp"
        f.write_bytes(b"\xff\xfe\xff\x0e" + "SketchUp Model{21.0.0}".encode("utf-16-le") + buf.getvalue())
        for args in (["info", str(f)], ["convert", str(f), "-o", str(self.tmp / "x.glb"), "-q"]):
            code, text = self._cli(*args)
            self.assertEqual(code, 1, text)
            self.assertIsNone(fuzz.pruefe_meldung(code, text), text)
            self.assertIn("beschaedigt", text)
        self.assertFalse((self.tmp / "x.glb").exists())

    def test_info_on_truncated_file_is_german(self):
        # Regression: info zeigte "SkpParseError: legacy .skp parse failed: unpack_from requires ..."
        f = self.tmp / "abgeschnitten.skp"
        f.write_bytes((ROOT / "samples" / "stuhl_tisch_2017.skp").read_bytes()[:20000])
        code, text = self._cli("info", str(f))
        self.assertEqual(code, 1)
        self.assertIsNone(fuzz.pruefe_meldung(code, text), text)
        self.assertIn("beschaedigt", text)

    def test_blender_error_is_one_line(self):
        from skptool import cli
        from skptool.blender import BlenderError
        grund = BlenderError("Blender-Schritt fehlgeschlagen:\nb.ply kuendigt 5 Elemente an")
        text = cli._explain(Path("b.ply"), grund, False)
        self.assertEqual(text, "Blender-Schritt fehlgeschlagen: b.ply kuendigt 5 Elemente an")
        roh = BlenderError("Blender-Schritt fehlgeschlagen:\nBlender 5.2\nTraceback (most recent call last):\n"
                           "  File \"x.py\", line 1\nRuntimeError: kaputt")
        text = cli._explain(Path("b.glb"), roh, False)
        self.assertNotIn("\n", text)
        self.assertNotIn("Traceback", text)


# ---------------------------------------------------------------- Mutator ist deterministisch

class TestMutator(unittest.TestCase):
    def test_deterministic(self):
        base = b"SketchUp" * 100
        a = fuzz.Mutator(123).mutate(base, runden=20)
        b = fuzz.Mutator(123).mutate(base, runden=20)
        self.assertEqual(a, b)
        c = fuzz.Mutator(124).mutate(base, runden=20)
        self.assertNotEqual(a, c)

    def test_strategies_never_crash_on_empty(self):
        m = fuzz.Mutator(1)
        for _ in range(200):
            m.mutate(b"", runden=5)  # darf nie werfen


# ---------------------------------------------------------------- kurze Rauch-Kampagne (< 60 s)

class TestFuzzCampaignSmoke(unittest.TestCase):
    """Faehrt die schnellen In-Prozess-Ziele von tools/fuzz.py mit festem Startwert und wenigen
    Faellen. Findet die Kampagne etwas, ist der Test rot und nennt den Ausloeser."""

    def test_inprocess_targets_clean(self):
        import argparse
        a = argparse.Namespace(seed=1, cases=18, blender_cases=0,
                               target="ops,refcheck,live", outdir=str(Path(tempfile.mkdtemp(prefix="fuzz_smoke_"))))
        k = fuzz.Kampagne(a)
        k.fahre(["ops", "refcheck", "live"])
        self.assertEqual(k.funde, [], f"Fuzz-Kampagne fand: {k.funde}")
        self.assertGreater(k.gezaehlt, 0)


if __name__ == "__main__":
    unittest.main()
