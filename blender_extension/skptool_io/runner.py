"""skptool finden und als eigenen Prozess starten. Ohne bpy, damit es auch ausserhalb von Blender testbar ist.

Sicherheit:
- Der Pfad zu skptool kommt nur aus den Add-on-Einstellungen (oder dem festen Starter-Ort im
  Benutzerordner), nie aus einer .blend-Datei.
- Gestartet wird immer mit einer Argumentliste, nie ueber eine Shell. Unter Windows wird ein
  .cmd/.bat nie direkt gestartet: cmd.exe wuerde Zeichen wie & oder % in Dateinamen auswerten.
  Stattdessen wird der Starter aufgeloest und die Python-Datei der Projekt-venv direkt aufgerufen.
- Python immer mit -P (nichts aus dem aktuellen Ordner importieren), Arbeitsordner ist der private
  Temp-Ordner des Auftrags.
Meldungen fuer die Blender-Oberflaeche sind englisch (wie Blender selbst), Kommentare deutsch.
"""
from __future__ import annotations

import os
import queue
import re
import signal
import subprocess
import threading
import time
from pathlib import Path

WINDOWS = os.name == "nt"
# Markierung der von tools/aufruf_einrichten.py angelegten Starter (muss dort gleich lauten)
STARTER_MARKE = "skptool-aufruf: angelegt von tools/aufruf_einrichten.py (Markierung, bitte nicht aendern)"
MAX_STARTER_BYTES = 4096
PROGRESS = re.compile(r"^\s*\[\s*([\d.]+)s\]\s*(.*)$")


class SkptoolNotFound(RuntimeError):
    pass


class Command:
    """Aufgeloester Aufruf: argv-Anfang, zusaetzliche Umgebung und eine lesbare Beschreibung."""

    def __init__(self, argv, env_extra=None, description=""):
        self.argv = list(argv)
        self.env_extra = dict(env_extra or {})
        self.description = description

    def __repr__(self):
        return f"Command({self.argv!r}, {self.env_extra!r})"


def default_starter() -> Path:
    """Wo tools/aufruf_einrichten.py den Starter anlegt."""
    if WINDOWS:
        base = os.environ.get("USERPROFILE") or str(Path.home())
        return Path(base) / ".local" / "bin" / "skptool.cmd"
    return Path.home() / ".local" / "bin" / "skptool"


def _venv_python(project: Path) -> Path:
    return project / ".venv" / ("Scripts/python.exe" if WINDOWS else "bin/python")


def _is_project(folder: Path) -> bool:
    return (folder / "skptool" / "__init__.py").is_file() and (folder / "skptool" / "cli.py").is_file()


def _python_command(python: Path, project: Path | None) -> Command:
    env = {"PYTHONPATH": str(project)} if project else {}
    desc = f"{python} -P -m skptool" + (f" (project {project})" if project else "")
    return Command([str(python), "-P", "-m", "skptool"], env, desc)


def _project_command(project: Path) -> Command:
    python = _venv_python(project)
    if not python.is_file():
        raise SkptoolNotFound(f"The skptool folder {project} has no venv ({python}), "
                              "see the skptool README (Installation)")
    return _python_command(python, project)


def _read_starter(path: Path) -> str | None:
    try:
        with open(path, "rb") as fh:
            head = fh.read(MAX_STARTER_BYTES).decode("utf-8", "replace")
    except OSError:
        return None
    return head if any(STARTER_MARKE in line for line in head.splitlines()[:5]) else None


def _starter_target(text: str) -> Path | None:
    """Projektordner aus einem Starter von tools/aufruf_einrichten.py (Formate inhalt_windows/inhalt_posix)."""
    if WINDOWS:
        m = re.search(r'^"([^"\r\n]+)" %\*\s*$', text, re.M)
        if m and m.group(1).lower().endswith("skptool.cmd"):
            return Path(m.group(1).replace("%%", "%")).parent
        return None
    m = re.search(r"^PYTHONPATH='([^'\n]*)'\s*$", text, re.M)
    return Path(m.group(1)) if m else None


def _looks_like_python(path: Path) -> bool:
    return re.fullmatch(r"python(\d+(\.\d+)?)?(\.exe)?", path.name.lower()) is not None


def resolve(setting: str) -> Command:
    """Einstellung "skptool path" in einen sicheren Aufruf uebersetzen.

    Erlaubt sind:
      - der Projektordner von skptool oder sein Starter skptool.cmd (Windows) bzw. skptool
      - ein von tools/aufruf_einrichten.py angelegter Starter (z. B. ~/.local/bin), mit Markierung
      - eine Python-Datei (z. B. .venv/Scripts/python.exe), die skptool importieren kann
      - ein installiertes skptool-Programm (.exe oder, ausser unter Windows, ein ausfuehrbares Skript)
    """
    setting = (setting or "").strip().strip('"')
    if not setting:
        raise SkptoolNotFound("No skptool path set (Preferences > Add-ons > SketchUp (.skp) via skptool)")
    path = Path(setting)
    if not path.is_absolute():
        raise SkptoolNotFound(f"The skptool path must be absolute: {setting}")
    if path.is_dir():
        if _is_project(path):
            return _project_command(path)
        raise SkptoolNotFound(f"{path} is not a skptool project folder")
    if not path.is_file():
        raise SkptoolNotFound(f"Not found: {path}")
    if _looks_like_python(path):
        # venv im Projektordner (<projekt>/.venv/Scripts/python.exe): Projekt in den PYTHONPATH
        project = next((p for p in path.parents if _is_project(p)), None) if ".venv" in path.parts else None
        return _python_command(path, project)
    if path.name.lower() in ("skptool.cmd", "skptool") and _is_project(path.parent):
        return _project_command(path.parent)
    text = _read_starter(path)
    if text is not None:
        project = _starter_target(text)
        if project is None or not _is_project(project):
            raise SkptoolNotFound(f"The launcher {path} does not point to a skptool project folder")
        return _project_command(project)
    ext = path.suffix.lower()
    if WINDOWS and ext in (".cmd", ".bat"):
        raise SkptoolNotFound(f"{path.name} is not started directly (cmd.exe would interpret characters like & "
                              "or % in file names). Set the skptool project folder, its skptool.cmd or "
                              ".venv\\Scripts\\python.exe instead")
    if WINDOWS and ext != ".exe":
        raise SkptoolNotFound(f"{path.name} is not an executable")
    if not WINDOWS and not os.access(path, os.X_OK):
        raise SkptoolNotFound(f"{path} is not executable")
    return Command([str(path)], {}, str(path))


def autodetect() -> str | None:
    """Starter am Standardort von tools/aufruf_einrichten.py, nur wenn er die Markierung traegt."""
    p = default_starter()
    if p.is_file() and _read_starter(p) is not None:
        try:
            resolve(str(p))
        except SkptoolNotFound:
            return None
        return str(p)
    return None


def build_env(cmd: Command) -> dict:
    env = dict(os.environ)
    for k in ("PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT", "PYTHONPATH", "PYTHONUSERBASE"):
        env.pop(k, None)
    env.update(cmd.env_extra)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _no_window():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if WINDOWS else {}


class Job:
    """Ein laufender Prozess mit Fortschrittszeilen (stderr) und Zeitlimit.

    poll() liefert None, solange er laeuft, sonst den Rueckgabewert. Die Ausgabe wird in Threads
    gelesen, damit Blender (Modal-Timer) nie blockiert."""

    def __init__(self, argv, env, cwd, timeout):
        self.argv = list(argv)
        self.timeout = timeout
        self.t0 = time.monotonic()
        self.stdout: list[str] = []
        self.stderr: list[str] = []
        self.last_step = ""
        self.timed_out = False
        self.cancelled = False
        self._lines: queue.Queue = queue.Queue()
        kw = {}
        if WINDOWS:  # kein Konsolenfenster, eigene Prozessgruppe
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
        self.proc = subprocess.Popen(self.argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, cwd=cwd, env=env, shell=False, **kw)
        self._threads = [threading.Thread(target=self._read, args=(self.proc.stdout, self.stdout), daemon=True),
                         threading.Thread(target=self._read, args=(self.proc.stderr, self.stderr), daemon=True)]
        for t in self._threads:
            t.start()

    def _read(self, stream, sink):
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            sink.append(line)
            self._lines.put(line)
        stream.close()

    @property
    def elapsed(self):
        return time.monotonic() - self.t0

    def progress(self) -> str:
        """Letzter Fortschrittsschritt von skptool (Zeilen wie '  [  1.2s] Lese x.skp ein')."""
        while True:
            try:
                line = self._lines.get_nowait()
            except queue.Empty:
                break
            m = PROGRESS.match(line)
            if m:
                self.last_step = m.group(2).strip()
        return self.last_step

    def poll(self):
        rc = self.proc.poll()
        if rc is None and self.timeout and self.elapsed > self.timeout:
            self.timed_out = True
            self.kill()
            rc = self.proc.poll()
            return -1 if rc is None else rc
        if rc is not None:
            for t in self._threads:
                t.join(timeout=5)
        return rc

    def wait(self, tick=0.2):
        while True:
            rc = self.poll()
            if rc is not None:
                return rc
            time.sleep(tick)

    def cancel(self):
        self.cancelled = True
        self.kill()

    def kill(self):
        """Den ganzen Prozessbaum beenden (skptool startet selbst Blender)."""
        if self.proc.poll() is not None:
            return
        try:
            if WINDOWS:
                root = os.environ.get("SystemRoot", r"C:\Windows")
                taskkill = os.path.join(root, "System32", "taskkill.exe")
                if os.path.isabs(taskkill) and os.path.isfile(taskkill):
                    subprocess.run([taskkill, "/PID", str(self.proc.pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, **_no_window())
            else:
                os.killpg(self.proc.pid, signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            self.proc.kill()
            self.proc.wait(timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass

    def error_text(self, limit=15) -> str:
        """Kurze Fehlerbeschreibung aus der Ausgabe von skptool."""
        if self.timed_out:
            return (f"skptool did not finish within {self.timeout} s and was stopped "
                    "(timeout in the add-on preferences)")
        if self.cancelled:
            return "Cancelled"
        lines = [l for l in self.stderr if l.strip() and not PROGRESS.match(l)]
        first = [l for l in lines if l.startswith("FEHLER")][-1:]
        rest = [l for l in lines if l not in first]
        text = "\n".join((first + rest)[:limit]) or "\n".join((self.stdout + self.stderr)[-limit:])
        return text or f"skptool exited with code {self.proc.returncode}"


def notices(job: Job) -> list[str]:
    """Hinweise von skptool (z. B. nicht uebernommene externe Dateien), samt eingerueckter Folgezeilen."""
    out, take = [], False
    for line in job.stderr:
        if line.startswith("Hinweis:"):
            out.append(line)
            take = True
        elif take and line.startswith("  ") and not PROGRESS.match(line):
            out.append(line)
        else:
            take = False
    return out


def version_of(cmd: Command, timeout=120) -> str:
    """skptool --version, fuer den Test-Knopf in den Einstellungen."""
    r = subprocess.run(cmd.argv + ["--version"], stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                       env=build_env(cmd), shell=False, **_no_window())
    out = (r.stdout + r.stderr).decode("utf-8", "replace").strip()
    if r.returncode != 0:
        raise SkptoolNotFound(out.splitlines()[-1] if out else f"exit code {r.returncode}")
    return out.splitlines()[0] if out else "?"
