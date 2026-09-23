"""Bearbeitungsoperationen (--ops) aus JSON-Text oder einer .json-Datei laden und grob pruefen.

Die genaue Pruefung jeder Operation macht blender_scripts/ops.py. Hier geht es um das, was vor
Blender passieren muss: Groesse, Dateiart, gueltiges JSON ohne NaN/Infinity, Anzahl."""
from __future__ import annotations

import json
from pathlib import Path

MAX_BYTES = 10 * 1024 * 1024
MAX_OPS = 1000
EXAMPLE = '[{"op": "move", "select": {"name": "Palme*"}, "by": [0, 0, 1]}]'


def _no_constants(name):
    raise ValueError(f"{name} ist keine gueltige Zahl")


def load_ops(raw: str) -> list:
    """JSON-Text (beginnt mit [ oder {) oder Pfad zu einer Datei. SystemExit mit deutscher Meldung."""
    if not isinstance(raw, str) or not raw.strip():
        raise SystemExit(f"--ops braucht eine Liste wie {EXAMPLE}")
    text = raw.strip()
    if text[0] not in "[{":
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
