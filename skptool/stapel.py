"""Stapelbetrieb mit mehreren Arbeitsprozessen: skptool convert "*.skp" -f glb -d out --jobs N.

Jede Datei laeuft in einem eigenen Prozess (python -P -m skptool convert <quelle> -o <ziel>), weil das
Einlesen mit OpenSKP rechenlastiges Python ist und viel Arbeitsspeicher braucht. Threads brachten
wegen der GIL nichts, und ein Absturz in einer Datei soll die anderen nicht mitreissen.

Speicherwaechter: Vor jedem Start wird die Speicherspitze der Datei aus ihrer Groesse geschaetzt.
Ein Arbeitsprozess startet nur, wenn die Summe der laufenden Schaetzungen hoechstens ANTEIL des beim
Start freien Arbeitsspeichers erreicht. Eine Datei, die allein schon darueber liegt, laeuft allein.

Jeder Arbeitsprozess schreibt in einen eigenen versteckten Ordner neben dem Ziel. Erst nach Erfolg
werden die Dateien an ihren Platz verschoben; bei Fehler, Zeitueberschreitung oder Strg+C bleibt
kein halbes Ergebnis liegen. Unter Windows haengt jeder Arbeitsprozess in einem Job-Objekt, das beim
Beenden auch Blender und den Python-Starter der venv mitnimmt; unter Linux und macOS in einer eigenen
Prozessgruppe.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from skptool import blender as _blender
from skptool import core

ANTEIL = 0.70  # hoechstens dieser Anteil des freien Arbeitsspeichers fuer alle Schaetzungen zusammen

# Speicherspitze je Datei im Arbeitsspeicher (Summe der Working-Set-Spitzen aller Prozesse im Baum),
# gemessen mit tools/speicher_messen.py unter Windows:
#   .skp -> .glb     16 KB und 30 KB: 58 MB; 0,74 MB: 76 MB; 5,25 MB: 552 MB (94 x Dateigroesse)
#   .skp -> .blend   Blender kommt dazu: 240 bis 400 MB (5,25 MB: 952 MB)
#   .blend -> .skp   3,55 MB: 594 MB (Blender liest, Python schreibt und liest zur Kontrolle neu ein)
# README: 22 MB .skp 2,3 GB (102 x), 201 MB .skp 12,4 GB (62 x). Der Faktor 100 deckt alle Messungen,
# die Grundwerte sind grosszuegig gerundet. Die Commit-Werte des Job-Objekts liegen hoeher (Python
# allein rund 515 MB, fast alles reservierter, nie beruehrter Speicher von numpy/OpenBLAS).
BASIS_PYTHON = 150 * 2**20
FAKTOR_SKP = 100
BASIS_BLENDER = 450 * 2**20
FAKTOR_ANDERE = 60  # .blend, .fbx, .glb ... als Eingabe (.blend ist gepackt und waechst beim Laden stark)

TMP_PREFIX = ".skptool-stapel-"
_OK_ZEILE = re.compile(r"^OK   .*\(\d+(?:\.\d+)?s, (.*)\)\s*$")


# ---------------------------------------------------------------- Arbeitsspeicher und Schaetzung

def freier_speicher() -> int | None:
    """Verfuegbarer Arbeitsspeicher in Byte, None wenn unbekannt."""
    try:
        if os.name == "nt":
            return _frei_windows()
        if sys.platform.startswith("linux"):
            return _frei_linux()
        if sys.platform == "darwin":
            return _frei_macos()
        pages = os.sysconf("SC_AVPHYS_PAGES")
        size = os.sysconf("SC_PAGE_SIZE")
        return pages * size if pages > 0 and size > 0 else None
    except (OSError, ValueError, AttributeError):
        return None


def _frei_windows() -> int | None:
    import ctypes
    from ctypes import wintypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    st = MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
        return None
    return int(st.ullAvailPhys) or None


def _frei_linux(pfad: str = "/proc/meminfo") -> int | None:
    werte = {}
    with open(pfad, encoding="ascii", errors="replace") as fh:
        for zeile in fh:
            name, _, rest = zeile.partition(":")
            teile = rest.split()
            if teile and teile[0].isdigit():
                werte[name.strip()] = int(teile[0]) * 1024  # Angabe in kB
    if "MemAvailable" in werte:
        return werte["MemAvailable"] or None
    frei = sum(werte.get(k, 0) for k in ("MemFree", "Buffers", "Cached"))  # Kernel vor 3.14
    return frei or None


def _frei_macos() -> int | None:
    """Freie, spekulative und inaktive Seiten laut vm_stat (so zaehlt auch die Aktivitaetsanzeige)."""
    out = subprocess.run(["/usr/bin/vm_stat"], capture_output=True, text=True, timeout=10).stdout
    m = re.search(r"page size of (\d+) bytes", out)
    size = int(m.group(1)) if m else os.sysconf("SC_PAGE_SIZE")
    seiten = 0
    for key in ("Pages free", "Pages speculative", "Pages inactive"):
        m = re.search(rf"^{key}:\s+(\d+)", out, re.M)
        if m:
            seiten += int(m.group(1))
    return seiten * size or None


def schaetzung(src: Path, dst: Path, mit_ops: bool = False) -> int:
    """Geschaetzte Speicherspitze in Byte fuer die Umwandlung src -> dst (ganzer Prozessbaum)."""
    size = Path(src).stat().st_size
    s_ext, d_ext = Path(src).suffix.lower(), Path(dst).suffix.lower()
    if s_ext == ".skp":
        est = BASIS_PYTHON + FAKTOR_SKP * size
        if mit_ops or d_ext not in core.NATIVE_FORMATS | {".skp"}:
            est += BASIS_BLENDER
    else:
        est = BASIS_PYTHON + BASIS_BLENDER + FAKTOR_ANDERE * size
    return est


def darf_starten(neu: int, laufend: list[int], budget: int | None, max_jobs: int) -> bool:
    """Speicherwaechter: startet ein weiterer Arbeitsprozess mit Schaetzung neu?

    Laeuft nichts, startet jede Datei, auch eine zu grosse (sie laeuft dann allein wie im
    Einzelbetrieb). Sonst nur, wenn noch ein Platz frei ist und die Summe aller Schaetzungen im
    Budget bleibt. Budget None (freier Speicher unbekannt): nie parallel."""
    if not laufend:
        return True
    if budget is None or len(laufend) >= max_jobs:
        return False
    return sum(laufend) + neu <= budget


def budget_aus(frei: int | None) -> int | None:
    return None if frei is None else int(frei * ANTEIL)


def kerne() -> int:
    """Nutzbare Kerne (ab Python 3.13 mit Beruecksichtigung der Prozessaffinitaet)."""
    fn = getattr(os, "process_cpu_count", None)
    if fn and fn():
        return fn()
    if hasattr(os, "sched_getaffinity"):
        return max(1, len(os.sched_getaffinity(0)))
    return os.cpu_count() or 1


def jobs_wert(text: str) -> int:
    """argparse-Typ fuer --jobs: ganze Zahl ab 0 oder auto (0 und auto: so viele wie Kerne)."""
    t = str(text).strip().lower()
    if t == "auto":
        return 0
    try:
        n = int(t)
    except ValueError:
        import argparse
        raise argparse.ArgumentTypeError(f"--jobs braucht eine Zahl ab 0 oder auto, nicht {text!r}") from None
    if n < 0:
        import argparse
        raise argparse.ArgumentTypeError(f"--jobs darf nicht negativ sein ({n})")
    return n


# ---------------------------------------------------------------- Prozessbaum beenden

class _WinJob:
    """Windows-Job-Objekt: alle Prozesse darin (venv-Starter, Python, Blender) lassen sich gemeinsam
    beenden, und schliesst der Elternprozess unerwartet, beendet Windows sie selbst (KILL_ON_JOB_CLOSE)."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self._c = ctypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                                ctypes.c_void_p]
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        self._k = k
        self.handle = k.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = _job_limits()()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            err = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(err)

    def assign(self, proc: subprocess.Popen) -> None:
        if not self._k.AssignProcessToJobObject(self.handle, int(proc._handle)):
            raise self._c.WinError(self._c.get_last_error())

    def active(self) -> int:
        acc = _job_accounting()()
        if not self._k.QueryInformationJobObject(self.handle, 1, self._c.byref(acc), self._c.sizeof(acc), None):
            return 0
        return int(acc.ActiveProcesses)

    def peak(self) -> int:
        """Hoechster gemeinsamer Speicherbedarf aller Prozesse im Job (Commit), in Byte."""
        info = _job_limits()()
        if not self._k.QueryInformationJobObject(self.handle, 9, self._c.byref(info), self._c.sizeof(info), None):
            return 0
        return int(info.PeakJobMemoryUsed)

    def terminate(self) -> None:
        if self.handle:
            self._k.TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        if self.handle:
            self._k.CloseHandle(self.handle)  # beendet uebrig gebliebene Prozesse (KILL_ON_JOB_CLOSE)
            self.handle = None


_STRUCTS: dict = {}


def _job_limits():
    if "limits" not in _STRUCTS:
        import ctypes
        from ctypes import wintypes

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOps", "WriteOps", "OtherOps", "ReadBytes",
                                                           "WriteBytes", "OtherBytes")]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        _STRUCTS["limits"] = EXTENDED
    return _STRUCTS["limits"]


def _job_accounting():
    if "acc" not in _STRUCTS:
        import ctypes
        from ctypes import wintypes

        class ACC(ctypes.Structure):
            _fields_ = [("TotalUserTime", ctypes.c_longlong), ("TotalKernelTime", ctypes.c_longlong),
                        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                        ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                        ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]

        _STRUCTS["acc"] = ACC
    return _STRUCTS["acc"]


class Prozess:
    """Ein Arbeitsprozess samt allem, was er startet (Blender)."""

    def __init__(self, cmd: list[str], env: dict, stdout, stderr):
        self._job = None
        kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": stdout, "stderr": stderr, "env": env,
                        "close_fds": True}
        if os.name == "nt":
            # eigene Prozessgruppe: Strg+C im Fenster erreicht nur den Elternprozess, der dann alle
            # Arbeitsprozesse geordnet beendet (sonst schreiben alle gleichzeitig "Abgebrochen")
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            try:
                self._job = _WinJob()
            except OSError:
                self._job = None  # dann beim Beenden taskkill /T
        else:
            kwargs["start_new_session"] = True
        try:
            self.proc = subprocess.Popen(cmd, **kwargs)
        except BaseException:
            self.schliessen()
            raise
        if self._job:
            try:
                self._job.assign(self.proc)
            except OSError:  # etwa in einer Umgebung, die keine verschachtelten Jobs erlaubt
                self.schliessen()

    def poll(self):
        return self.proc.poll()

    def peak(self) -> int:
        return self._job.peak() if self._job else 0

    def beenden(self, warten: float = 10.0) -> None:
        """Den ganzen Baum beenden und warten, bis nichts mehr laeuft."""
        if self._job:
            self._job.terminate()
            ende = time.time() + warten
            while self._job.active() and time.time() < ende:
                time.sleep(0.02)
        elif os.name == "nt":
            if self.proc.poll() is None:
                taskkill = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")
                try:
                    subprocess.run([taskkill, "/F", "/T", "/PID", str(self.proc.pid)], capture_output=True,
                                   timeout=max(warten, 5.0))
                except (OSError, subprocess.TimeoutExpired):
                    self.proc.kill()
        else:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                self.proc.wait(timeout=min(3.0, warten))
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)  # auch Blender, falls der Arbeitsprozess schon weg ist
            except (ProcessLookupError, PermissionError):
                pass
        try:
            self.proc.wait(timeout=warten)
        except subprocess.TimeoutExpired:
            pass

    def schliessen(self) -> None:
        if self._job:
            self._job.close()
            self._job = None


# ---------------------------------------------------------------- Arbeitsprozesse

# Schalter aus convert, die jeder Arbeitsprozess braucht. Alles andere ist hier bewusst ausgelassen:
# Ziele plant der Elternprozess (-o je Datei), -q ist immer gesetzt, --jobs gilt nur hier.
_FLAGS = {"no_textures": "--no-textures", "keep_triangles": "--keep-triangles", "verbose": "-v",
          "verify": "--verify", "allow_external": "--allow-external", "no_verify": "--no-verify"}
_WERTE = {"blender": "--blender", "unit_scale": "--unit-scale", "width": "--width", "height": "--height"}
IGNORIERT = {"inputs", "output", "format", "outdir", "force", "jobs", "quiet", "ops", "func", "cmd"}


def worker_args(a, ops_pfad: Path | None) -> list[str]:
    """Durchgereichte Schalter fuer einen Arbeitsprozess (ohne Quelle und Ziel)."""
    args = ["-q"]
    for key, flag in _FLAGS.items():
        if getattr(a, key, False):
            args.append(flag)
    for key, flag in _WERTE.items():
        val = getattr(a, key, None)
        if val is not None:
            args += [flag, repr(val) if isinstance(val, float) else str(val)]
    if ops_pfad is not None:
        args += ["--ops", str(ops_pfad)]
    return args


def worker_cmd(src: Path, dst: Path, args: list[str]) -> list[str]:
    # -P: nie Module aus dem aktuellen Ordner laden (wie skptool.cmd); das Paket kommt ueber PYTHONPATH
    return [sys.executable, "-P", "-m", "skptool", "convert", str(src), "-o", str(dst), *args]


def worker_env() -> dict:
    """Gleiche Umgebung, dazu der Ordner dieses skptool-Pakets vorn im PYTHONPATH (nur absolute Eintraege,
    ein leerer oder relativer stuende fuer den aktuellen Ordner)."""
    root = str(Path(__file__).resolve().parent.parent)
    rest = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p and os.path.isabs(p)]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([root] + [p for p in rest if os.path.normcase(p) != os.path.normcase(root)])
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _entfernen(pfad: Path) -> None:
    """Ordner loeschen; Windows gibt Dateien eines eben beendeten Prozesses manchmal erst kurz danach frei."""
    for _ in range(50):
        shutil.rmtree(pfad, ignore_errors=True)
        if not pfad.exists():
            return
        time.sleep(0.1)


def _uebernehmen(quelle: Path, ziel: Path) -> None:
    """Alles aus dem Arbeitsordner an seinen Platz verschieben (mit Nebendateien wie .mtl, .bin, Texturen)."""
    for item in sorted(quelle.iterdir()):
        dest = ziel / item.name
        if item.is_dir() and dest.is_dir():
            _uebernehmen(item, dest)
        else:
            os.replace(item, dest)


class _Auftrag:
    def __init__(self, nr: int, src: Path, dst: Path, est: int):
        self.nr, self.src, self.dst, self.est = nr, src, dst, est
        self.proc: Prozess | None = None
        self.arbeit: Path | None = None
        self.start = 0.0
        self.out = self.err = None


def _text(pfad: Path) -> str:
    try:
        return pfad.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _fehlertext(auftrag: _Auftrag, rc: int, out: str, err: str) -> str:
    zeilen = [z for z in err.splitlines() if z.strip()]
    kopf = f"FEHLER {auftrag.src.name}: "
    for z in zeilen:
        if z.startswith(kopf):
            return z[len(kopf):]
    if rc not in (0, 1, 2):
        if os.name == "nt" and (rc < 0 or rc > 255):
            code = f"Rueckgabewert 0x{rc & 0xFFFFFFFF:08X}"  # z. B. 0xC0000005 Zugriffsverletzung
        elif rc < 0:
            code = f"Signal {-rc}"
        else:
            code = f"Rueckgabewert {rc}"
        letzte = f" Letzte Ausgabe: {zeilen[-1]}" if zeilen else ""
        return f"Arbeitsprozess abgestuerzt ({code}).{letzte}"
    if zeilen:
        return zeilen[-1]
    return f"Arbeitsprozess ohne Meldung beendet (Rueckgabewert {rc})"


def _dateien(n: int) -> str:
    return f"{n} {'Datei' if n == 1 else 'Dateien'}"


def ausfuehren(plan: list[tuple[Path, Path]], a, jobs: int) -> int | None:
    """Plan parallel abarbeiten. Rueckgabe wie cmd_convert, None: nicht parallel moeglich (dann
    arbeitet der Aufrufer wie bisher nacheinander)."""
    ops = None
    if getattr(a, "ops", None):  # einmal hier pruefen und lesen (stdin geht nur einmal), vor jedem Start
        from skptool.opsjson import load_ops
        ops = load_ops(a.ops)
    frei = freier_speicher()
    quiet = getattr(a, "quiet", False)
    if frei is None:
        print("Hinweis: freier Arbeitsspeicher unbekannt, wandle nacheinander um.", file=sys.stderr)
        return None
    budget = budget_aus(frei)
    max_jobs = min(jobs if jobs > 0 else kerne(), len(plan))
    mit_ops = bool(getattr(a, "ops", None))
    wartend = [_Auftrag(i, src, dst, schaetzung(src, dst, mit_ops)) for i, (src, dst) in enumerate(plan)]
    wartend.sort(key=lambda x: -x.est)  # grosse zuerst: sie laufen allein, kleine fuellen danach auf
    if not quiet:
        print(f"Stapel: {_dateien(len(plan))}, bis zu {max_jobs} gleichzeitig, Speicherbudget "
              f"{budget / 2**30:.1f} GB ({ANTEIL:.0%} von {frei / 2**30:.1f} GB frei)", file=sys.stderr, flush=True)
    timeout = _blender.BLENDER_TIMEOUT
    t0 = time.time()
    ok = fehler = 0
    laufend: list[_Auftrag] = []

    with tempfile.TemporaryDirectory(prefix="skptool_stapel_", ignore_cleanup_errors=True) as tmp:
        tmp = Path(tmp)
        ops_pfad = None
        if ops is not None:
            import json

            ops_pfad = tmp / "ops.json"
            ops_pfad.write_text(json.dumps(ops), encoding="utf-8")
        args = worker_args(a, ops_pfad)
        env = worker_env()

        def starten(auf: _Auftrag) -> None:
            auf.dst.parent.mkdir(parents=True, exist_ok=True)
            auf.arbeit = auf.dst.parent / f"{TMP_PREFIX}{uuid.uuid4().hex[:12]}"
            auf.arbeit.mkdir()
            auf.out, auf.err = tmp / f"{auf.nr}.out", tmp / f"{auf.nr}.err"
            with open(auf.out, "wb") as fo, open(auf.err, "wb") as fe:
                auf.proc = Prozess(worker_cmd(auf.src.resolve(), auf.arbeit / auf.dst.name, args), env, fo, fe)
            auf.start = time.time()

        def abschliessen(auf: _Auftrag, rc: int | None, grund: str | None = None) -> bool:
            out, err = _text(auf.out), _text(auf.err)
            dauer = time.time() - auf.start
            fertig = auf.arbeit / auf.dst.name
            try:
                if grund is None and rc == 0:
                    if not fertig.is_file() or fertig.stat().st_size == 0:
                        grund = f"{auf.dst} wurde nicht geschrieben"
                    else:
                        _uebernehmen(auf.arbeit, auf.dst.parent)
                if grund is None and rc != 0:
                    grund = _fehlertext(auf, rc, out, err)
            except OSError as exc:
                grund = f"Ergebnis liess sich nicht an seinen Platz verschieben: {exc}"
            finally:
                auf.proc.schliessen()
                _entfernen(auf.arbeit)
            kopf = f"FEHLER {auf.src.name}: "
            rest_err = [z for z in err.splitlines() if z.strip() and not z.startswith(kopf)]
            if getattr(a, "verbose", False):  # Blender-Zeiten usw. geschlossen vor der Ergebniszeile der Datei
                for z in out.splitlines():
                    if z.strip() and not _OK_ZEILE.match(z):
                        print(z)
            if grund is None:
                how = next((m.group(1) for m in map(_OK_ZEILE.match, out.splitlines()) if m), "fertig")
                print(f"OK   {auf.src.name} -> {auf.dst}  ({dauer:.1f}s, {how})", flush=True)
            if grund is None and rest_err:  # Hinweise, z. B. auf nicht uebernommene externe Dateien
                print("\n".join(rest_err), file=sys.stderr, flush=True)
            if grund is not None:
                if getattr(a, "verbose", False) and rest_err:
                    print("\n".join(rest_err), file=sys.stderr)
                print(f"{kopf}{grund}", file=sys.stderr, flush=True)
            return grund is None

        def aufraeumen(auf: _Auftrag) -> None:
            try:
                if auf.proc is not None:
                    auf.proc.beenden()
            finally:
                if auf.proc is not None:
                    auf.proc.schliessen()
                if auf.arbeit is not None:
                    _entfernen(auf.arbeit)

        # Strg+Pause (Windows) und SIGTERM (Linux, macOS) wie Strg+C behandeln: alle Arbeitsprozesse
        # geordnet beenden statt den Elternprozess sofort sterben zu lassen
        alte_handler = []

        def _abbruch(signum, frame):
            raise KeyboardInterrupt

        for name in (("SIGBREAK",) if os.name == "nt" else ("SIGTERM",)):
            sig = getattr(signal, name, None)
            if sig is not None:
                try:
                    alte_handler.append((sig, signal.signal(sig, _abbruch)))
                except ValueError:  # nicht im Hauptthread
                    pass
        try:
            while wartend or laufend:
                while wartend and darf_starten(wartend[0].est, [x.est for x in laufend], budget, max_jobs):
                    auf = wartend.pop(0)
                    laufend.append(auf)  # vor dem Start, damit ein Abbruch ihn sicher mit beendet
                    try:
                        starten(auf)
                    except OSError as exc:
                        laufend.remove(auf)
                        aufraeumen(auf)
                        fehler += 1
                        print(f"FEHLER {auf.src.name}: Arbeitsprozess liess sich nicht starten: {exc}",
                              file=sys.stderr, flush=True)
                for auf in list(laufend):
                    rc = auf.proc.poll()
                    grund = None
                    if rc is None:
                        if time.time() - auf.start <= timeout:
                            continue
                        auf.proc.beenden()
                        rc = auf.proc.poll()
                        grund = (f"nach {timeout} s nicht fertig und beendet (Grenze je Datei anhebbar mit "
                                 "SKPTOOL_TIMEOUT in Sekunden)")
                    else:
                        auf.proc.beenden(warten=5.0)  # uebrig gebliebene Kindprozesse (Blender) nie stehen lassen
                    laufend.remove(auf)
                    if abschliessen(auf, rc, grund):
                        ok += 1
                    else:
                        fehler += 1
                time.sleep(0.05)
        except KeyboardInterrupt:
            n = len(laufend)
            ruhig = []  # ein zweites Strg+C darf das Aufraeumen nicht unterbrechen
            for sig in (signal.SIGINT, getattr(signal, "SIGBREAK", None) or getattr(signal, "SIGTERM", None)):
                try:
                    ruhig.append((sig, signal.signal(sig, signal.SIG_IGN)))
                except (ValueError, TypeError):
                    pass
            try:
                while laufend:
                    aufraeumen(laufend.pop())
            finally:
                for sig, alt in ruhig:
                    signal.signal(sig, alt if alt is not None else signal.SIG_DFL)
            print(f"\nAbgebrochen nach {time.time() - t0:.1f}s: {ok} OK, {fehler} FEHLER, "
                  f"{n} beendet, {len(wartend)} nicht begonnen. Keine halben Dateien.",
                  file=sys.stderr, flush=True)
            raise
        finally:
            while laufend:  # nur bei unerwartetem Fehler noch belegt
                aufraeumen(laufend.pop())
            for sig, alt in alte_handler:
                signal.signal(sig, alt if alt is not None else signal.SIG_DFL)
            _entfernen(tmp)  # Protokolldateien; Windows gibt sie nach dem Beenden manchmal erst spaeter frei

    print(f"Fertig: {ok} OK, {fehler} FEHLER von {_dateien(len(plan))} in {time.time() - t0:.1f}s "
          f"(bis zu {max_jobs} gleichzeitig)", flush=True)
    return 1 if fehler else 0
