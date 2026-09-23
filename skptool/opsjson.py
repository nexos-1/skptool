"""Bearbeitungsoperationen (--ops) aus JSON-Text, einer .json-Datei oder stdin (-) laden und grob pruefen.

Die genaue Pruefung jeder Operation macht blender_scripts/ops.py. Hier geht es um das, was vor
Blender passieren muss: Groesse, Dateiart, gueltiges JSON ohne NaN/Infinity, Anzahl."""
from __future__ import annotations

import json
import sys
from pathlib import Path

MAX_BYTES = 10 * 1024 * 1024
MAX_OPS = 1000
EXAMPLE = '[{"op": "move", "select": {"name": "Palme*"}, "by": [0, 0, 1]}]'

_stdin_text: str | None = None  # stdin laesst sich nur einmal lesen (convert mit mehreren Eingaben)


def _no_constants(name):
    raise ValueError(f"{name} ist keine gueltige Zahl")


def _read_stream(stream) -> str:
    data = stream.read(MAX_BYTES + 1)
    if isinstance(data, str):
        data = data.encode("utf-8")
    if len(data) > MAX_BYTES:
        raise SystemExit(f"--ops -: Eingabe ist groesser als {MAX_BYTES // 2**20} MB")
    # Windows PowerShell 5.1 mit $OutputEncoding = UTF8 schickt die BOM zweimal
    return data.decode("utf-8", "replace").lstrip("\ufeff")


def _read_stdin(stream=None) -> str:
    """--ops -: JSON aus stdin, binaer gelesen, hoechstens MAX_BYTES."""
    global _stdin_text
    if stream is not None:
        return _read_stream(stream)
    if _stdin_text is None:
        if sys.stdin is None:
            raise SystemExit("--ops -: keine Standardeingabe vorhanden")
        _stdin_text = _read_stream(getattr(sys.stdin, "buffer", sys.stdin))
    return _stdin_text


def load_ops(raw: str, stdin=None) -> list:
    """JSON-Text (beginnt mit [ oder {), Pfad zu einer Datei oder "-" fuer stdin.
    SystemExit mit deutscher Meldung. stdin: anderer Datenstrom statt sys.stdin (fuer Tests)."""
    if not isinstance(raw, str) or not raw.strip():
        raise SystemExit(f"--ops braucht eine Liste wie {EXAMPLE}")
    text = raw.strip()
    if text == "-":
        text = _read_stdin(stdin).strip()
        if not text:
            raise SystemExit(f"--ops -: stdin ist leer, erwartet wird eine Liste wie {EXAMPLE}")
    elif text[0] not in "[{":
        path = Path(raw)
        if not path.is_file():
            raise SystemExit(f"--ops: Datei nicht gefunden (oder keine normale Datei): {raw}")
        if path.stat().st_size > MAX_BYTES:
            raise SystemExit(f"--ops: Datei ist groesser als {MAX_BYTES // 2**20} MB")
        with path.open("rb") as fh:
            data = fh.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise SystemExit(f"--ops: Datei ist groesser als {MAX_BYTES // 2**20} MB")
        text = data.decode("utf-8-sig", "replace")
    elif len(text.encode("utf-8")) > MAX_BYTES:
        raise SystemExit(f"--ops ist groesser als {MAX_BYTES // 2**20} MB")
    try:
        data = json.loads(text, parse_constant=_no_constants)
    except (ValueError, RecursionError) as exc:
        raise SystemExit(f"--ops ist kein gueltiges JSON: {str(exc)[:200]}") from None
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or not all(isinstance(o, dict) and isinstance(o.get("op"), str) for o in data):
        raise SystemExit(f"--ops braucht eine Liste wie {EXAMPLE}")
    if len(data) > MAX_OPS:
        raise SystemExit(f"--ops: hoechstens {MAX_OPS} Operationen ({len(data)} angegeben)")
    return data
