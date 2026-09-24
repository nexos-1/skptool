"""Stapelbetrieb mit mehreren Prozessen (convert --jobs). Start:
.venv\\Scripts\\python -m unittest tests.test_parallel -v
"""
import io
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from skptool import cli, stapel
from skptool.blender import BlenderError, find_blender

ROOT = Path(__file__).resolve().parents[1]
S2017 = ROOT / "samples" / "stuhl_tisch_2017.skp"
S2025 = ROOT / "samples" / "leer_2025.skp"
GB = 2**30
MB = 2**20

try:
    find_blender()
    HAVE_BLENDER = True
except BlenderError:
    HAVE_BLENDER = False


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


def prozesse_mit(text: str) -> list[str]:
    """Befehlszeilen aller laufenden Prozesse, die text enthalten (ohne diese Abfrage selbst)."""
    if os.name == "nt":
        ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and "
              "$_.CommandLine.Contains($env:SKPTOOL_SUCHE) -and $_.ProcessId -ne $PID } | "
              "ForEach-Object { \"$($_.ProcessId) $($_.CommandLine)\" }")
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=300,
                           env=dict(os.environ, SKPTOOL_SUCHE=text))
        return [z for z in r.stdout.splitlines() if z.strip()]
    if not shutil.which("ps") and os.path.isdir("/proc"):  # schlanke Linux-Container haben kein ps
        zeilen = []
        for eintrag in os.listdir("/proc"):
            if not eintrag.isdigit() or int(eintrag) == os.getpid():
                continue
            try:
                with open(f"/proc/{eintrag}/cmdline", "rb") as fh:
                    befehl = fh.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
            except OSError:
                continue
            if befehl and text in befehl:  # Zombies haben eine leere Befehlszeile
                zeilen.append(f"{eintrag} {befehl}")
        return zeilen
    r = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True, timeout=60)
    return [z for z in r.stdout.splitlines() if text in z and "ps -eo" not in z]


def lebt(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if os.path.isdir("/proc/self"):  # Linux: Zustand aus /proc (auch ohne ps); Zombie zaehlt nicht
        try:
            with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as fh:
                return fh.read().rpartition(")")[2].split()[0] != "Z"
        except FileNotFoundError:
            return False  # inzwischen weg
        except (OSError, IndexError):
            return True
    try:  # macOS: Zombie (beendet, noch nicht abgeholt) zaehlt nicht
        return "Z" not in subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True,
                                         text=True).stdout
    except OSError:
        return True


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="skptool_stapel_test_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def batch(self, names_and_sources, folder="in"):
        d = self.tmp / folder
        d.mkdir(exist_ok=True)
        for name, src in names_and_sources:
            if isinstance(src, bytes):
                (d / name).write_bytes(src)
            else:
                shutil.copy(src, d / name)
        return d

    def assert_no_leftovers(self, folder: Path):
        rest = [p.name for p in folder.rglob("*") if p.name.startswith(stapel.TMP_PREFIX)]
        self.assertEqual(rest, [], "Arbeitsordner nicht aufgeraeumt")


# ---------------------------------------------------------------- Speicherwaechter, reine Funktionen

class TestSpeicherwaechter(unittest.TestCase):
    def test_first_file_always_starts_even_if_too_big(self):
        self.assertTrue(stapel.darf_starten(50 * GB, [], budget=4 * GB, max_jobs=8))
        self.assertTrue(stapel.darf_starten(50 * GB, [], budget=None, max_jobs=8))

    def test_big_file_runs_alone(self):
        # 200-MB-Datei (Schaetzung rund 20 GB) bei 16 GB frei: nichts daneben, auch keine kleine
        budget = stapel.budget_aus(16 * GB)
        self.assertEqual(budget, int(16 * GB * 0.7))
        self.assertFalse(stapel.darf_starten(200 * MB, [20 * GB], budget, max_jobs=8))
        self.assertFalse(stapel.darf_starten(20 * GB, [200 * MB], budget, max_jobs=8))

    def test_sum_of_estimates_stays_within_budget(self):
        budget = stapel.budget_aus(10 * GB)  # 7 GB
        self.assertTrue(stapel.darf_starten(2 * GB, [2 * GB, 2 * GB], budget, max_jobs=8))  # 6 GB
        self.assertFalse(stapel.darf_starten(2 * GB, [2 * GB, 2 * GB, 2 * GB], budget, max_jobs=8))  # 8 GB
        self.assertTrue(stapel.darf_starten(1 * GB, [2 * GB, 2 * GB, 2 * GB], budget, max_jobs=8))  # genau 7 GB

    def test_job_limit_and_unknown_memory(self):
        self.assertFalse(stapel.darf_starten(1, [1, 1], 100 * GB, max_jobs=2))
        self.assertTrue(stapel.darf_starten(1, [1], 100 * GB, max_jobs=2))
        self.assertFalse(stapel.darf_starten(1, [1], None, max_jobs=8))  # unbekannt: nacheinander

    def test_estimate_from_file_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "gross.skp"
            with open(src, "wb") as fh:
                fh.truncate(20 * MB)
            blend = Path(tmp) / "gross.blend"
            with open(blend, "wb") as fh:
                fh.truncate(20 * MB)
            glb = stapel.schaetzung(src, Path("x.glb"))
            self.assertEqual(glb, stapel.BASIS_PYTHON + stapel.FAKTOR_SKP * 20 * MB)
            self.assertAlmostEqual(glb / GB, 2.1, delta=0.2)  # README: 22 MB brauchen 2,3 GB
            self.assertEqual(stapel.schaetzung(src, Path("x.blend")), glb + stapel.BASIS_BLENDER)
            self.assertEqual(stapel.schaetzung(src, Path("x.skp")), glb)
            self.assertEqual(stapel.schaetzung(src, Path("x.glb"), mit_ops=True), glb + stapel.BASIS_BLENDER)
            self.assertGreater(stapel.schaetzung(blend, Path("x.skp")), stapel.BASIS_BLENDER)

    def test_free_memory_readers(self):
        frei = stapel.freier_speicher()
        if frei is not None:
            self.assertGreater(frei, 64 * MB)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "meminfo"
            p.write_text("MemTotal:       16000000 kB\nMemFree:         1000000 kB\n"
                         "MemAvailable:    8000000 kB\nCached:          5000000 kB\n", encoding="ascii")
            self.assertEqual(stapel._frei_linux(str(p)), 8000000 * 1024)
            p.write_text("MemTotal: 16000000 kB\nMemFree: 1000 kB\nBuffers: 2000 kB\nCached: 3000 kB\n",
                         encoding="ascii")
            self.assertEqual(stapel._frei_linux(str(p)), 6000 * 1024)

    def test_cgroup_grenze_im_container(self):
        """Linux im Container: /proc/meminfo zeigt den ganzen Rechner, die Grenze steht in der cgroup.
        Gefunden bei der Linux-Pruefung in Docker (--memory): ohne das plante --jobs auto nach dem
        freien Speicher des Rechners statt nach der Grenze des Containers."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            selbst = tmp / "cgroup_self"

            def datei(pfad, text):
                pfad.parent.mkdir(parents=True, exist_ok=True)
                pfad.write_text(text, encoding="ascii")

            # v2 mit eigenem Namensraum: eigene cgroup ist die Wurzel des Mounts
            v2 = tmp / "v2"
            datei(selbst, "0::/\n")
            datei(v2 / "memory.max", "2147483648\n")
            datei(v2 / "memory.current", str(700 * MB) + "\n")
            datei(v2 / "memory.stat", f"anon 1000\nfile 5000\ninactive_file {200 * MB}\nactive_file 7\n")
            self.assertEqual(stapel._frei_cgroup(str(v2), str(selbst)), 2 * GB - 500 * MB)
            # keine Grenze
            datei(v2 / "memory.max", "max\n")
            self.assertIsNone(stapel._frei_cgroup(str(v2), str(selbst)))
            # v2 unter systemd: eine uebergeordnete cgroup hat die engere Grenze
            datei(selbst, "0::/user.slice/app.scope\n")
            datei(v2 / "user.slice" / "memory.max", str(1 * GB))
            datei(v2 / "user.slice" / "memory.current", str(100 * MB))
            datei(v2 / "user.slice" / "app.scope" / "memory.max", str(4 * GB))
            datei(v2 / "user.slice" / "app.scope" / "memory.current", str(50 * MB))
            self.assertEqual(stapel._frei_cgroup(str(v2), str(selbst)), 1 * GB - 100 * MB)
            # belegt ueber der Grenze: 0, nie negativ
            datei(v2 / "user.slice" / "memory.current", str(2 * GB))
            self.assertEqual(stapel._frei_cgroup(str(v2), str(selbst)), 0)
            # Pfad mit .. (ausserhalb des Namensraums): nur die Wurzel zaehlt
            datei(selbst, "0::/../../fremd\n")
            self.assertIsNone(stapel._frei_cgroup(str(v2), str(selbst)))

            # v1 ohne cgroup-Namensraum: /proc/self/cgroup nennt den Pfad des Rechners, im Mount ist die
            # eigene cgroup aber die Wurzel
            v1 = tmp / "v1"
            datei(selbst, "12:cpu,cpuacct:/docker/abc\n11:memory:/docker/abc\n0::/system.slice/x\n")
            datei(v1 / "memory" / "memory.limit_in_bytes", str(3 * GB))
            datei(v1 / "memory" / "memory.usage_in_bytes", str(1 * GB))
            datei(v1 / "memory" / "memory.stat", f"cache 5\ntotal_inactive_file {256 * MB}\n")
            self.assertEqual(stapel._frei_cgroup(str(v1), str(selbst)), 2 * GB + 256 * MB)
            datei(v1 / "memory" / "memory.limit_in_bytes", "9223372036854771712")  # v1: keine Grenze
            self.assertIsNone(stapel._frei_cgroup(str(v1), str(selbst)))
            # fehlende oder kaputte Dateien: None statt Fehler
            self.assertIsNone(stapel._frei_cgroup(str(tmp / "fehlt"), str(tmp / "fehlt_auch")))
            datei(selbst, "Unsinn\n\n0::\n")
            self.assertIsNone(stapel._frei_cgroup(str(tmp / "leer"), str(selbst)))

        # unter Linux gilt der kleinere Wert
        with mock.patch.object(stapel.sys, "platform", "linux"), mock.patch.object(stapel.os, "name", "posix"), \
                mock.patch.object(stapel, "_frei_linux", return_value=30 * GB), \
                mock.patch.object(stapel, "_frei_cgroup", return_value=2 * GB):
            self.assertEqual(stapel.freier_speicher(), 2 * GB)
        with mock.patch.object(stapel.sys, "platform", "linux"), mock.patch.object(stapel.os, "name", "posix"), \
                mock.patch.object(stapel, "_frei_linux", return_value=3 * GB), \
                mock.patch.object(stapel, "_frei_cgroup", return_value=None):
            self.assertEqual(stapel.freier_speicher(), 3 * GB)


class TestJobsSchalter(unittest.TestCase):
    def parse(self, *args):
        err = io.StringIO()
        with redirect_stderr(err):
            try:
                return cli.build_parser().parse_args(["convert", "a.skp", "-f", "glb", *args]).jobs, ""
            except SystemExit as exc:
                return exc.code, err.getvalue()

    def test_valid_values(self):
        self.assertEqual(self.parse()[0], 1)
        self.assertEqual(self.parse("--jobs", "4")[0], 4)
        self.assertEqual(self.parse("-j", "2")[0], 2)
        self.assertEqual(self.parse("--jobs", "0")[0], 0)
        self.assertEqual(self.parse("--jobs", "auto")[0], 0)
        self.assertEqual(self.parse("--jobs", "AUTO")[0], 0)

    def test_invalid_values(self):
        for bad in ("-1", "-5", "zwei", "1.5", ""):
            code, err = self.parse(f"--jobs={bad}")
            self.assertEqual(code, 2, bad)
            self.assertIn("--jobs", err)
        self.assertIn("negativ", self.parse("--jobs=-1")[1])

    def test_every_convert_option_is_passed_to_workers_or_deliberately_not(self):
        ap = cli.build_parser()
        convert = ap._subparsers._group_actions[0].choices["convert"]
        dests = {a.dest for a in convert._actions if a.dest != "help"}
        known = set(stapel._FLAGS) | set(stapel._WERTE) | stapel.IGNORIERT
        self.assertEqual(dests - known, set(), "neuer Schalter: in stapel.worker_args durchreichen oder ignorieren")

    def test_worker_args(self):
        a = cli.build_parser().parse_args(["convert", "a.skp", "-f", "blend", "--no-textures", "--width", "320",
                                           "--unit-scale", "0.01", "--blender", "C:/b/blender.exe", "-v",
                                           "--allow-external", "--jobs", "3", "--force"])
        args = stapel.worker_args(a, Path("ops.json"))
        self.assertEqual(args[0], "-q")
        for part in (["--no-textures"], ["--width", "320"], ["--unit-scale", "0.01"], ["-v"], ["--allow-external"],
                     ["--blender", "C:/b/blender.exe"], ["--ops", "ops.json"]):
            i = args.index(part[0])
            self.assertEqual(args[i:i + len(part)], part)
        for absent in ("--jobs", "--force", "-f", "-d", "--verify", "--keep-triangles"):
            self.assertNotIn(absent, args)
        cmd = stapel.worker_cmd(Path("q.skp"), Path("z.blend"), args)
        self.assertEqual(cmd[:5], [sys.executable, "-P", "-m", "skptool", "convert"])
        env = stapel.worker_env()
        self.assertEqual(env["PYTHONPATH"].split(os.pathsep)[0], str(ROOT))
        self.assertTrue(all(os.path.isabs(p) for p in env["PYTHONPATH"].split(os.pathsep)))


# ---------------------------------------------------------------- echte Arbeitsprozesse

class TestParallel(Tmp):
    def test_same_bytes_as_sequential(self):
        src = self.batch([("a.skp", S2017), ("b.skp", S2017), ("c.skp", S2025), ("d.skp", S2025)])
        code, out, err = run_cli("convert", str(src / "*.skp"), "-f", "glb", "-d", str(self.tmp / "seq"), "-q")
        self.assertEqual(code, 0, err)
        t = time.time()
        code, out, err = run_cli("convert", str(src / "*.skp"), "-f", "glb", "-d", str(self.tmp / "par"),
                                 "--jobs", "2")
        self.assertEqual(code, 0, err)
        self.assertLess(time.time() - t, 300)
        self.assertEqual(sum(z.startswith("OK   ") for z in out.splitlines()), 4, out)
        self.assertIn("Fertig: 4 OK, 0 FEHLER von 4 Dateien", out)
        self.assertIn("Stapel: 4 Dateien, bis zu 2 gleichzeitig", err)
        self.assertNotIn("[", err, "Fortschrittszeilen der Arbeitsprozesse gehoeren nicht in die Ausgabe")
        seq = sorted(p.name for p in (self.tmp / "seq").iterdir())
        par = sorted(p.name for p in (self.tmp / "par").iterdir())
        self.assertEqual(seq, ["a.glb", "b.glb", "c.glb", "d.glb"])
        self.assertEqual(par, seq)
        for name in seq:
            self.assertEqual((self.tmp / "par" / name).read_bytes(), (self.tmp / "seq" / name).read_bytes(), name)
        self.assertEqual(prozesse_mit(str(self.tmp)), [])

    def test_broken_file_fails_alone(self):
        src = self.batch([("gut1.skp", S2017), ("kaputt.skp", os.urandom(4000)), ("gut2.skp", S2025)])
        code, out, err = run_cli("convert", str(src / "*.skp"), "-f", "glb", "-d", str(self.tmp / "out"),
                                 "--jobs", "3", "-q")
        self.assertEqual(code, 1)
        self.assertIn("FEHLER kaputt.skp: keine SketchUp-Datei", err)
        self.assertEqual(err.count("FEHLER"), 1, err)
        self.assertIn("Fertig: 2 OK, 1 FEHLER von 3 Dateien", out)
        self.assertEqual(sorted(p.name for p in (self.tmp / "out").iterdir()), ["gut1.glb", "gut2.glb"])
        self.assertNotIn("Stapel:", err)  # -q

    def test_checks_happen_before_any_worker(self):
        for d in ("d1", "d2", "out"):
            (self.tmp / d).mkdir()
        shutil.copy(S2017, self.tmp / "d1" / "x.skp")
        shutil.copy(S2017, self.tmp / "d2" / "x.skp")
        shutil.copy(S2025, self.tmp / "d1" / "y.skp")
        shutil.copy(S2025, self.tmp / "d1" / "y_konvertiert.skp")
        with mock.patch.object(stapel, "Prozess", side_effect=AssertionError("Arbeitsprozess gestartet")) as p:
            # doppeltes Ziel
            code, _, err = run_cli("convert", str(self.tmp / "d1" / "x.skp"), str(self.tmp / "d2" / "x.skp"),
                                   "-f", "glb", "-d", str(self.tmp / "out"), "-j", "2", "-q")
            self.assertNotEqual(code, 0)
            self.assertIn("beide", err)
            # Ziel ist selbst eine Eingabe: y.skp -> y_konvertiert.skp
            code, _, err = run_cli("convert", str(self.tmp / "d1" / "y*.skp"), "-f", "skp", "-j", "2", "-q")
            self.assertNotEqual(code, 0)
            self.assertIn("selbst eine Eingabe", err)
            # vorhandenes Ziel ohne --force
            precious = self.tmp / "out" / "y.glb"
            precious.write_bytes(b"wertvoll")
            code, _, err = run_cli("convert", str(self.tmp / "d1" / "*.skp"), "-f", "glb", "-d",
                                   str(self.tmp / "out"), "-j", "2", "-q")
            self.assertNotEqual(code, 0)
            self.assertIn("--force", err)
            # ungueltige --ops
            code, _, err = run_cli("convert", str(self.tmp / "d1" / "*.skp"), "-f", "glb", "-d",
                                   str(self.tmp / "neu"), "-j", "2", "-q", "--force", "--ops", "[kaputt")
            self.assertNotEqual(code, 0)
            self.assertIn("--ops", err)
            p.assert_not_called()
        self.assertEqual(precious.read_bytes(), b"wertvoll")
        self.assertEqual([p.name for p in (self.tmp / "out").iterdir()], ["y.glb"])
        self.assertFalse((self.tmp / "neu").exists())

    def test_memory_guard_limits_concurrency(self):
        src = self.batch([(f"s{i}.skp", S2017) for i in range(4)])
        seen = []
        echt = stapel.darf_starten

        def spy(neu, laufend, budget, max_jobs):
            ok = echt(neu, laufend, budget, max_jobs)
            if ok:
                seen.append(len(laufend) + 1)
            return ok

        est = stapel.schaetzung(src / "s0.skp", Path("x.glb"))
        # frei nur fuer eine Schaetzung (70 % von 1,2 x Schaetzung): alles laeuft nacheinander
        with mock.patch.object(stapel, "freier_speicher", return_value=int(est * 1.2)), \
                mock.patch.object(stapel, "darf_starten", side_effect=spy):
            code, out, err = run_cli("convert", str(src / "*.skp"), "-f", "glb", "-d", str(self.tmp / "a"), "-j", "4")
        self.assertEqual(code, 0, err)
        self.assertEqual(max(seen), 1)
        self.assertIn("bis zu 4 gleichzeitig", err)
        seen.clear()
        # Platz fuer genau zwei
        with mock.patch.object(stapel, "freier_speicher", return_value=int(est * 2 / 0.7) + 1), \
                mock.patch.object(stapel, "darf_starten", side_effect=spy):
            code, out, err = run_cli("convert", str(src / "*.skp"), "-f", "glb", "-d", str(self.tmp / "b"), "-j", "4")
        self.assertEqual(code, 0, err)
        self.assertEqual(max(seen), 2)

    def test_unknown_memory_falls_back_to_sequential(self):
        src = self.batch([("a.skp", S2017), ("b.skp", S2025)])
        with mock.patch.object(stapel, "freier_speicher", return_value=None), \
                mock.patch.object(stapel, "Prozess", side_effect=AssertionError("Arbeitsprozess gestartet")):
            code, out, err = run_cli("convert", str(src / "*.skp"), "-f", "glb", "-d", str(self.tmp / "o"), "-j", "0",
                                     "-q")
        self.assertEqual(code, 0, err)
        self.assertIn("nacheinander", err)
        self.assertEqual(sorted(p.name for p in (self.tmp / "o").iterdir()), ["a.glb", "b.glb"])

    def test_single_file_and_default_stay_sequential(self):
        src = self.batch([("a.skp", S2017), ("b.skp", S2025)])
        with mock.patch.object(stapel, "ausfuehren", side_effect=AssertionError("parallel")):
            code, out, err = run_cli("convert", str(src / "*.skp"), "-f", "glb", "-d", str(self.tmp / "o"), "-q")
            self.assertEqual(code, 0, err)
            code, out, err = run_cli("convert", str(src / "a.skp"), "-o", str(self.tmp / "e.glb"), "-j", "4", "-q")
            self.assertEqual(code, 0, err)
        self.assertNotIn("Fertig:", out)


# ---------------------------------------------------------------- Absturz, Zeitgrenze, Strg+C

GOOD_WORKER = "import sys, pathlib; p = pathlib.Path(sys.argv[sys.argv.index('-o') + 1]); p.write_bytes(b'gut')"
CRASH_WORKER = "import faulthandler; faulthandler._read_null()"  # echte Zugriffsverletzung wie in einer C-Bibliothek
# Arbeitsprozess, der ein Kind startet (wie Blender) und beide ewig warten laesst; die pid des Kindes landet
# in der Datei hinter --pidfile
HANG_WORKER = ("import subprocess, sys, time, pathlib; "
               "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); "
               "pathlib.Path(sys.argv[sys.argv.index('--pidfile') + 1]).write_text(str(c.pid)); "
               "time.sleep(120)")


class TestFehlerfaelle(Tmp):
    def fake_workers(self, pidfile):
        def cmd(src, dst, args):
            name = Path(src).name
            code = {"absturz.skp": CRASH_WORKER, "haengt.skp": HANG_WORKER}.get(name, GOOD_WORKER)
            # Marker in der Befehlszeile, damit die Suche nach Waisen die Prozesse findet
            return [sys.executable, "-c", code, "-o", str(dst), "--pidfile", str(pidfile), str(self.tmp)]
        return cmd

    def test_crash_and_timeout_fail_only_that_file(self):
        src = self.batch([("gut1.skp", S2017), ("absturz.skp", S2017), ("haengt.skp", S2017), ("gut2.skp", S2025)])
        pidfile = self.tmp / "kind.pid"
        with mock.patch.object(stapel, "worker_cmd", side_effect=self.fake_workers(pidfile)), \
                mock.patch.object(stapel._blender, "BLENDER_TIMEOUT", 20):
            t = time.time()
            code, out, err = run_cli("convert", str(src / "*.skp"), "-f", "glb", "-d", str(self.tmp / "out"),
                                     "-j", "4", "-q")
            dauer = time.time() - t
        self.assertEqual(code, 1)
        self.assertLess(dauer, 120)
        self.assertIn("FEHLER absturz.skp: Arbeitsprozess abgestuerzt", err)
        self.assertIn("0xC0000005" if os.name == "nt" else "Signal 11", err)
        self.assertIn("FEHLER haengt.skp: nach 20 s nicht fertig und beendet", err)
        self.assertIn("SKPTOOL_TIMEOUT", err)
        self.assertIn("Fertig: 2 OK, 2 FEHLER von 4 Dateien", out)
        self.assertEqual(sorted(p.name for p in (self.tmp / "out").iterdir()), ["gut1.glb", "gut2.glb"])
        self.assertEqual((self.tmp / "out" / "gut1.glb").read_bytes(), b"gut")
        kind = int(pidfile.read_text())
        self.assertFalse(lebt(kind), "Kindprozess des haengenden Arbeitsprozesses laeuft noch")
        self.assertEqual(prozesse_mit(str(self.tmp)), [])

    @unittest.skipUnless(os.name == "nt", "nur Windows")
    def test_tree_is_killed_even_without_job_object(self):
        src = self.batch([("haengt.skp", S2017), ("gut.skp", S2025)])
        pidfile = self.tmp / "kind.pid"
        with mock.patch.object(stapel, "worker_cmd", side_effect=self.fake_workers(pidfile)), \
                mock.patch.object(stapel._WinJob, "assign", side_effect=OSError("verschachtelte Jobs verboten")), \
                mock.patch.object(stapel._blender, "BLENDER_TIMEOUT", 15):
            code, out, err = run_cli("convert", str(src / "*.skp"), "-f", "glb", "-d", str(self.tmp / "out"),
                                     "-j", "2", "-q")
        self.assertEqual(code, 1)
        self.assertIn("FEHLER haengt.skp: nach 15 s nicht fertig", err)
        self.assertIn("Fertig: 1 OK, 1 FEHLER", out)
        self.assertFalse(lebt(int(pidfile.read_text())), "taskkill /T hat das Kind nicht beendet")
        self.assertEqual(prozesse_mit(str(self.tmp)), [])

    def test_worker_that_writes_nothing(self):
        src = self.batch([("a.skp", S2017), ("b.skp", S2025)])
        with mock.patch.object(stapel, "worker_cmd",
                               side_effect=lambda s, d, a: [sys.executable, "-c", "pass", str(self.tmp)]):
            code, out, err = run_cli("convert", str(src / "*.skp"), "-f", "glb", "-d", str(self.tmp / "out"),
                                     "-j", "2", "-q")
        self.assertEqual(code, 1)
        self.assertIn("wurde nicht geschrieben", err)
        self.assertEqual(list((self.tmp / "out").iterdir()), [])


@unittest.skipUnless(HAVE_BLENDER, "Blender nicht gefunden")
class TestAbbruch(Tmp):
    """Strg+C (unter Windows als CTRL_BREAK_EVENT, sonst SIGINT) an einen echten Stapellauf mit Blender."""

    def test_interrupt_stops_all_workers_and_leaves_no_partial_files(self):
        src = self.batch([(f"s{i}.skp", S2017) for i in range(4)])
        out = self.tmp / "out"
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else \
            {"start_new_session": True}
        p = subprocess.Popen([sys.executable, "-P", "-m", "skptool", "convert", str(src / "*.skp"), "-f", "blend",
                              "-d", str(out), "-j", "2"], cwd=self.tmp, env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, **kwargs)
        try:
            # warten, bis Blender fuer beide ersten Dateien laeuft
            ende = time.time() + 120
            while time.time() < ende and p.poll() is None:
                if sum("blender" in z.lower() for z in prozesse_mit(str(out))) >= 2:
                    break
                time.sleep(0.5)
            self.assertIsNone(p.poll(), "Stapel war schon fertig, bevor abgebrochen werden konnte")
            self.assertGreaterEqual(sum("blender" in z.lower() for z in prozesse_mit(str(out))), 2)
            if os.name == "nt":
                os.kill(p.pid, signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(p.pid, signal.SIGINT)  # wie Strg+C im Terminal: an die ganze Vordergrundgruppe
            stdout, stderr = p.communicate(timeout=60)
        finally:
            if p.poll() is None:
                p.kill()
        stderr = stderr.decode("utf-8", "replace")
        self.assertEqual(p.returncode, 130, stderr)
        self.assertIn("Abgebrochen nach", stderr)
        self.assertIn("Keine halben Dateien", stderr)
        self.assert_no_leftovers(out)
        for f in out.iterdir():  # was fertig wurde, ist vollstaendig
            self.assertEqual(f.suffix, ".blend")
            self.assertGreater(f.stat().st_size, 1000)
        time.sleep(1)
        self.assertEqual(prozesse_mit(str(self.tmp)), [], "Waisen nach Abbruch")


if __name__ == "__main__":
    unittest.main()
