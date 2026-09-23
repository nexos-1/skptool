"""Speicherspitze einer Umwandlung messen (Grundlage fuer die Schaetzung in skptool/stapel.py).

  python tools/speicher_messen.py samples/stuhl_tisch_2017.skp samples/extern/*.skp -f glb blend

Jede Umwandlung laeuft wie im Stapelbetrieb als eigener Arbeitsprozess. Windows: hoechster gemeinsamer
Speicherbedarf aller Prozesse im Job-Objekt (Python und Blender zusammen). Linux, macOS: groesste
Spitze eines einzelnen Kindprozesses (ru_maxrss), also eher zu niedrig, wenn Blender mitlaeuft.
"""
from __future__ import annotations

import argparse
import glob
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from skptool import stapel  # noqa: E402


def _working_sets(job) -> dict:
    """Windows: PeakWorkingSetSize je Prozess im Job (pid -> Byte)."""
    import ctypes
    from ctypes import wintypes

    class PMC(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (n, ctypes.c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                           "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                                           "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]

    class IDS(ctypes.Structure):
        _fields_ = [("Assigned", wintypes.DWORD), ("Listed", wintypes.DWORD), ("Ids", ctypes.c_size_t * 64)]

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = wintypes.HANDLE
    k.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                            ctypes.c_void_p]
    k.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    ids = IDS()
    out = {}
    if not k.QueryInformationJobObject(job.handle, 3, ctypes.byref(ids), ctypes.sizeof(ids), None):
        return out
    for pid in ids.Ids[: ids.Listed]:
        h = k.OpenProcess(0x1000 | 0x0010, False, int(pid))  # QUERY_LIMITED_INFORMATION | VM_READ
        if not h:
            continue
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        if k.K32GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
            out[int(pid)] = int(pmc.PeakWorkingSetSize)
        k.CloseHandle(h)
    return out


def messen(src: Path, fmt: str) -> tuple[int, int, float, int]:
    """Rueckgabe: Commit-Spitze des Jobs, Summe der Working-Set-Spitzen, Sekunden, Rueckgabewert."""
    with tempfile.TemporaryDirectory(prefix="skptool_messen_") as tmp:
        dst = Path(tmp) / f"{src.stem}.{fmt}"
        cmd = stapel.worker_cmd(src.resolve(), dst, ["-q"])
        t = time.time()
        ws: dict = {}
        with open(Path(tmp) / "log", "wb") as log:
            p = stapel.Prozess(cmd, stapel.worker_env(), log, log)
            while p.poll() is None:
                if p._job:
                    ws.update(_working_sets(p._job))
                time.sleep(0.05)
            peak = p.peak()
            p.beenden()
            p.schliessen()
        if not peak and os.name != "nt":
            import resource
            peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * (1 if sys.platform == "darwin" else 1024)
        return peak, sum(ws.values()) or peak, time.time() - t, p.proc.returncode


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("-f", "--formats", nargs="+", default=["glb"])
    a = ap.parse_args()
    files = [Path(h) for item in a.inputs for h in (glob.glob(item) or [item])]
    print(f"{'Datei':28} {'MB':>8} {'Ziel':6} {'Commit MB':>10} {'WS MB':>8} {'Schaetzung':>10} {'Zeit':>6}  rc")
    for src in files:
        size = src.stat().st_size
        for fmt in a.formats:
            peak, ws, dauer, rc = messen(src, fmt)
            est = stapel.schaetzung(src, Path(f"x.{fmt}"))
            print(f"{src.name[:28]:28} {size / 2**20:8.2f} {fmt:6} {peak / 2**20:10.0f} {ws / 2**20:8.0f} "
                  f"{est / 2**20:10.0f} {dauer:6.1f}  {rc}")


if __name__ == "__main__":
    main()
