"""skptool - SketchUp-Dateien ohne SketchUp lesen, konvertieren, in Blender bearbeiten
und wieder als .skp speichern.

Beispiele:
  skptool info haus.skp
  skptool convert haus.skp -o haus.blend          # zum Bearbeiten in Blender
  skptool convert haus.skp -o haus.glb            # glTF, direkt ohne Blender
  skptool convert haus.skp -o haus.3mf            # 3D-Druck (Slicer), Millimeter, ohne Blender
  skptool convert *.skp -f fbx -d export\\         # Stapelkonvertierung
  skptool convert *.skp -f glb -d export\\ -j auto # dasselbe mit mehreren Prozessen gleichzeitig
  skptool open haus.skp                           # .blend erzeugen und Blender oeffnen
  skptool convert haus.blend -o haus_neu.skp      # nach dem Bearbeiten zurueck nach SketchUp
  skptool convert haus_2026.skp -o haus_2017.skp  # neue Datei ins 2017-Format umschreiben
  skptool render haus.skp -o vorschau.png
  skptool list haus.skp --name "Palme*"             # Objekte finden
  skptool edit haus.skp -o haus_neu.skp --ops '[{"op": "scale", "select": {"name": "Palme*"}, "factor": 1.2}]'
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from skptool import __version__
from skptool import core, stapel
from openskp import SkpFile

from skptool.blender import BlenderError, launch_gui, run_bridge
from skptool.opsjson import load_ops


def _expand(inputs: list[str]) -> list[Path]:
    paths: list[Path] = []
    for item in inputs:
        hits = glob.glob(item) if any(c in item for c in "*?[") else [item]
        if not hits:
            raise SystemExit(f"Keine Datei passt zu: {item}")
        paths += [Path(h) for h in hits]
    for p in paths:
        if not p.exists():
            raise SystemExit(f"Datei nicht gefunden: {p}")
    return paths


def _print_info(data: dict, show_all: bool) -> None:
    limit = None if show_all else 25

    def cut(items):
        return items if limit is None else items[:limit]

    print(f"Datei:        {data['file']}  ({data['size_bytes'] / 1024:.0f} KB)")
    print(f"Version:      {data['version']}  ->  {data['format']}")
    if "size_m" in data:
        s = data["size_m"]
        print(f"Abmessungen:  {s['width']} x {s['depth']} x {s['height']} m (B x T x H), "
              f"{data['triangles']} Dreiecke")
    print(f"Flaechen:     {data['faces_total']}")
    print(f"Ebenen/Tags ({len(data['layers'])}):")
    for l in data["layers"]:
        print(f"  - {l['name']}{'  [ausgeblendet]' if l['hidden'] else ''}")
    print(f"Materialien ({len(data['materials'])}):")
    for mt in cut(data["materials"]):
        rgba = mt["rgba"]
        color = "#%02x%02x%02x" % tuple(rgba[:3]) if rgba else "-"
        print(f"  - {mt['name']}  {color}{'  [Textur]' if mt['textured'] else ''}")
    comps = data["components"]
    print(f"Komponenten und Gruppen ({len(comps)}):")
    for c in cut(comps):
        print(f"  - {c['name']}  {c['instances']}x platziert, {c['faces']} Flaechen")
    hidden = len(data["materials"]) + len(comps) - len(cut(data["materials"])) - len(cut(comps))
    if hidden > 0:
        print(f"  ... {hidden} weitere Eintraege, alle anzeigen mit --all")
    if data["scenes"]:
        print(f"Szenen: {', '.join(data['scenes'])}")


def cmd_info(a) -> int:
    rc = 0
    for p in _expand(a.inputs):
        big = p.stat().st_size > 50 * 1024 * 1024
        bounds = a.bounds or (not a.fast and not big)
        if big and not bounds and not a.json:
            print(f"Hinweis: {p.name} ist gross, Abmessungen nur mit --bounds (braucht viel Arbeitsspeicher).")
        try:
            data = core.info(p, with_bounds=bounds)
        except Exception as exc:
            print(f"FEHLER {p}: {_explain(p, exc, False)}", file=sys.stderr)
            rc = 1
            continue
        if a.json:
            print(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            _print_info(data, a.all)
            print()
    return rc


def _same_file(a: Path, b: Path) -> bool:
    """Auch ueber andere Schreibweisen hinweg (lange Windows-Pfade, Freigaben auf localhost, Links)."""
    try:
        if a.resolve() == b.resolve():
            return True
        return a.exists() and b.exists() and os.path.samefile(a, b)
    except OSError:
        return False


def _target_for(src: Path, a) -> Path:
    if a.output:
        return Path(a.output)
    fmt = a.format.lower().lstrip(".")
    folder = Path(a.outdir) if a.outdir else src.parent
    target = folder / f"{src.stem}.{fmt}"
    if target.resolve() == src.resolve():
        target = folder / f"{src.stem}_konvertiert.{fmt}"
    return target


def _report_external(stats: dict, a) -> None:
    files = stats.get("external_files") or []
    if not files:
        return
    what = ("aus Sicherheitsgruenden nicht uebernommen (nur bei sicherer Quelle: --allow-external)"
            if stats.get("external_dropped") else "uebernommen (--allow-external)")
    print(f"Hinweis: {len(files)} Verweis(e) auf externe Dateien {what}:", file=sys.stderr)
    for f in files[: None if a.verbose else 5]:
        print(f"  {f}", file=sys.stderr)


class _Progress:
    """Schritte mit Laufzeit auf stderr, damit lange Laeufe nicht stumm wirken."""

    def __init__(self, quiet: bool):
        self.quiet = quiet
        self.t0 = time.time()

    def __call__(self, msg: str) -> None:
        if not self.quiet:
            print(f"  [{time.time() - self.t0:6.1f}s] {msg}", file=sys.stderr, flush=True)


def _read_hint(src: Path) -> str:
    """Grobe Zeitschaetzung fuers Einlesen: gemessen etwa 1 bis 1,5 s pro MB."""
    mb = src.stat().st_size / 2**20
    if mb < 5:
        return f"Lese {src.name} ein"
    if mb > 60:
        return f"Lese {src.name} ein ({mb:.0f} MB, dauert grob {mb / 60:.0f} bis {mb * 1.5 / 60 + 0.5:.0f} min)"
    return f"Lese {src.name} ein ({mb:.0f} MB, dauert grob {mb:.0f} bis {mb * 1.5:.0f} s)"


def _ops_file(a, tmp: Path):
    """--ops als JSON-Text oder Pfad zu einer JSON-Datei; Rueckgabe: Pfad fuer die Bridge oder None."""
    raw = getattr(a, "ops", None)
    if not raw:
        return None
    data = load_ops(raw)
    out = tmp / "ops.json"
    out.write_text(json.dumps(data), encoding="utf-8")
    return out


def _remember_ops(a, stats):
    if stats and stats.get("ops") is not None:
        a.ops_results = stats["ops"]


def _skp_into_blender(src: Path, outs, a, tmp: Path, step, ops=None) -> dict:
    """SketchUp-Datei einlesen, in Blender aufbereiten, optional bearbeiten, Ausgaben schreiben."""
    step(_read_hint(src))
    skp = core.open_skp(src)
    glb_path, meta = tmp / "model.glb", tmp / "meta.json"
    step("Baue Komponenten und Platzierungen auf")
    core.export_for_blender(skp, glb_path, meta, textures=not a.no_textures)
    step("Blender uebernimmt" + (" und bearbeitet" if ops else ""))
    args = ["import", "--glb", str(glb_path), "--meta", str(meta),
            "--width", str(a.width), "--height", str(a.height)]
    for o in outs:
        args += ["--out", str(Path(o).resolve())]
    if a.keep_triangles:
        args.append("--keep-triangles")
    if ops:
        args += ["--ops", str(ops)]
    res = run_bridge(args, blender=a.blender, verbose=a.verbose)
    _remember_ops(a, res["stats"])
    return res["stats"]


def convert_one(src: Path, dst: Path, a) -> str:
    s_ext, d_ext = src.suffix.lower(), dst.suffix.lower()
    if _same_file(dst, src):
        raise SystemExit(f"Ziel und Quelle sind dieselbe Datei: {src}")
    textures = not a.no_textures
    step = _Progress(getattr(a, "quiet", False))
    with tempfile.TemporaryDirectory(prefix="skptool_") as tmp:
        tmp = Path(tmp)
        ops = _ops_file(a, tmp)
        if s_ext == ".skp" and ops is not None:
            if d_ext in core.BLENDER_OUT_FORMATS:  # direkt: einlesen, bearbeiten, schreiben
                st = _skp_into_blender(src, [dst], a, tmp, step, ops)
                return (f"bearbeitet ueber Blender: {st['objects']} Objekte, {st['faces']} Flaechen")
            # sonst erst bearbeitete Zwischendatei, dann normaler Weg ab .blend
            edited = tmp / "bearbeitet.blend"
            _skp_into_blender(src, [edited], a, tmp, step, ops)
            src, s_ext, ops = edited, ".blend", None
        if s_ext == ".skp":
            if d_ext == ".skp":
                step(_read_hint(src) + " und schreibe sie im 2017-Format neu")
                st = core.rewrite_legacy(src, dst)
                for zeile in st.get("verluste", []):  # je verlorener Art eine Zeile, wie in skptool report
                    print(f"Hinweis {src.name}: {zeile}", file=sys.stderr)
                return (f"als SketchUp-2017-Datei geschrieben ({st['version']}), "
                        f"{st['faces_written']} von {st['faces_source']} Flaechen, "
                        f"{st['triangulated']} trianguliert, {st['skipped']} uebersprungen, "
                        f"{len(st['warnings'])} Hinweise")
            if d_ext in core.NATIVE_FORMATS:
                step(_read_hint(src))
                skp = core.open_skp(src)
                step(f"Schreibe {dst.name}")
                core.export_native(skp, dst, textures=textures)
                return "direkt mit OpenSKP"
            if d_ext in core.BLENDER_OUT_FORMATS:
                st = _skp_into_blender(src, [dst], a, tmp, step)
                return (f"ueber Blender: {st['objects']} Objekte, {st['faces']} Flaechen, "
                        f"{st['materials']} Materialien, Ebenen: {', '.join(st['collections']) or '-'}")
            raise SystemExit(f"Zielformat {d_ext} wird nicht unterstuetzt")
        if s_ext not in core.BLENDER_IN_FORMATS:
            raise SystemExit(f"Quellformat {s_ext} wird nicht unterstuetzt")
        extra = (["--allow-external"] if a.allow_external else []) + (["--ops", str(ops)] if ops else [])
        if d_ext == ".skp":
            header = tmp / "dump.json"
            step(f"Blender liest {src.name}" + (" und bearbeitet" if ops else ""))
            res = run_bridge(["dump", "--in", str(src.resolve()), "--json", str(header),
                              "--images", str(tmp / "images")] + extra,
                             blender=a.blender, verbose=a.verbose)
            _report_external(res["stats"], a)
            _remember_ops(a, res["stats"])
            # Blender-Einheit -> Zoll: Meter pro Einheit durch Meter pro Zoll (vorher 1/unit_scale, damit
            # wurde --unit-scale 1.0 zu "1 Einheit = 1 Zoll" statt 1 Meter)
            scale = a.unit_scale / core.INCH if a.unit_scale else None
            step(f"Schreibe {dst.name}")
            st = core.write_skp_from_bin(header, dst, scale_to_inch=scale, textures=textures, reparse=False)
            size_mb = dst.stat().st_size / 2**20
            check = ""
            if a.verify or (not a.no_verify and size_mb <= 100):
                step("Lese das Ergebnis zur Kontrolle neu ein")
                model = SkpFile.open(str(dst)).parse()
                check = f", beim Gegenlesen {core.placed_face_count(model)} platzierte Flaechen"
            return (f"als SketchUp-2017-Datei geschrieben ({size_mb:.0f} MB): {st['groups']} Gruppen, "
                    f"{st['components']} Komponenten mit {st['instances']} Platzierungen, {st['faces']} Flaechen, "
                    f"{st['textured_materials']} Texturmaterialien, {st['triangulated']} trianguliert, "
                    f"{st['skipped']} uebersprungen{check}")
        if d_ext not in core.BLENDER_OUT_FORMATS | {".glb", ".obj", ".stl", ".ply"}:  # wie bridge.write_any
            raise SystemExit(f"Zielformat {d_ext} wird nicht unterstuetzt")
        step(f"Blender konvertiert {src.name} nach {dst.name}")
        res = run_bridge(["load", "--in", str(src.resolve()), "--out", str(dst.resolve()),
                          "--width", str(a.width), "--height", str(a.height)] + extra,
                         blender=a.blender, verbose=a.verbose)
        _report_external(res["stats"], a)
        _remember_ops(a, res["stats"])
        return f"ueber Blender: {res['stats']['objects']} Objekte"


def _explain(src: Path, exc: Exception, verbose: bool) -> str:
    """Fehler verstaendlich einordnen; technische Details (Hex-Auszuege) nur mit -v."""
    name, msg = type(exc).__name__, str(exc)
    if name == "BlenderError":
        # Eine Zeile ohne Klassennamen: run_bridge setzt den Grund in eine zweite Zeile, und ohne
        # Ergebniszeile stehen dort die letzten Zeilen der Blender-Ausgabe (auch ein Traceback).
        zeilen = [z.strip() for z in msg.splitlines() if z.strip()]
        kopf = "Blender-Schritt fehlgeschlagen:"
        if zeilen and zeilen[0] == kopf:
            rest = zeilen[1:]
            if any(z.startswith("Traceback") for z in rest) or len(rest) > 3:
                rest = rest[-1:]  # die eigentliche Meldung steht zuletzt, alles mit -v
            return f"{kopf} {' '.join(rest)}" if rest else kopf
        return " ".join(zeilen)
    # Typische Folgen kaputter Daten beim Lesen (fehlender Eintrag, Index hinter dem Ende, ungueltiger
    # Text): ohne englischen Ausnahmenamen melden. Echte Programmfehler (AttributeError, TypeError)
    # bleiben sichtbar.
    beschaedigt = ("KeyError", "IndexError", "UnicodeDecodeError", "OverflowError", "EOFError", "NotImplementedError")
    if src.suffix.lower() == ".skp" and name in beschaedigt:
        try:
            version = core.header_version(src)
        except (OSError, ValueError):
            return "keine SketchUp-Datei (Dateikopf fehlt)"
        detail = f" Technisch: {name}: {msg}" if verbose else " Details mit -v."
        return f"Datei ist beschaedigt oder unvollstaendig (Version {version}).{detail}"
    if src.suffix.lower() == ".skp" and name in ("SkpParseError", "BadZipFile", "error", "ValueError"):
        try:
            version = core.header_version(src)
        except ValueError:
            return "keine SketchUp-Datei (Dateikopf fehlt)"
        detail = f" Technisch: {name}: {msg}" if verbose else " Details mit -v."
        # "requires a buffer ..." / "unpack": struct lief ueber das Dateiende (abgeschnittene Datei)
        kaputt = ("truncat", "unexpected end", "requires a buffer", "unpack")
        if name in ("BadZipFile", "error") or any(k in msg.lower() for k in kaputt):
            return f"Datei ist beschaedigt oder unvollstaendig (Version {version}).{detail}"
        if name == "SkpParseError":
            return (f"OpenSKP kann diese Datei (Version {version}) nicht lesen. Bei aelteren Versionen hilft "
                    f"der RedHalo Sketchup_Importer in Blender, siehe README.{detail}")
    return f"{name}: {msg}" if name not in ("ValueError", "UnsafeFileError") else msg


def cmd_convert(a) -> int:
    srcs = _expand(a.inputs)
    if a.output and len(srcs) > 1:
        raise SystemExit("Bei mehreren Eingaben -f/--format (und optional -d) statt -o verwenden")
    if not a.output and not a.format:
        raise SystemExit("Ziel fehlt: -o DATEI oder -f FORMAT angeben")
    plan = [(src, _target_for(src, a)) for src in srcs]
    seen: dict = {}
    for src, dst in plan:
        key = os.path.normcase(str(dst.resolve()))
        if key in seen:
            raise SystemExit(f"{seen[key].name} und {src.name} ergaeben beide {dst}. Nichts geschrieben.")
        seen[key] = src
        clash = next((s for s in srcs if _same_file(dst, s)), None)
        if clash is not None:
            raise SystemExit(f"Ziel {dst} ist selbst eine Eingabe ({clash.name}). Nichts geschrieben.")
        if len(plan) > 1 and dst.exists() and not getattr(a, "force", False):
            raise SystemExit(f"{dst} gibt es schon. Ueberschreiben mit --force, sonst anderen Zielordner (-d). "
                             "Nichts geschrieben.")
    jobs = getattr(a, "jobs", 1)
    if len(plan) > 1 and jobs != 1:  # alle Pruefungen oben sind durch, erst jetzt startet ein Arbeitsprozess
        rc = stapel.ausfuehren(plan, a, jobs)
        if rc is not None:
            return rc
    rc = 0
    for src, dst in plan:
        t = time.time()
        try:
            how = convert_one(src, dst, a)
            if not dst.is_file() or dst.stat().st_size == 0:
                raise RuntimeError(f"{dst} wurde nicht geschrieben")
            print(f"OK   {src.name} -> {dst}  ({time.time() - t:.1f}s, {how})")
        except (BlenderError, Exception) as exc:  # noqa: B014 - klare Meldung je Datei
            rc = 1
            print(f"FEHLER {src.name}: {_explain(src, exc, a.verbose)}", file=sys.stderr)
    return rc


def _print_ops(results) -> None:
    for r in results or []:
        if not r.get("ok"):
            print(f"  FEHLER {r['op']}: {r.get('error')}")
            continue
        detail = {k: v for k, v in r.items() if k not in ("op", "ok", "objects", "names")}
        print(f"  {r['op']:12} " + ", ".join(f"{k}={v}" for k, v in detail.items()))


def cmd_edit(a) -> int:
    if not a.ops:
        raise SystemExit("edit braucht --ops, z. B. --ops "
                         "'[{\"op\": \"scale\", \"select\": {\"name\": \"Palme*\"}, \"factor\": 1.2}]'")
    a.inputs, a.format, a.outdir = [a.input], None, None
    rc = cmd_convert(a)
    if rc == 0:
        print("Bearbeitung:")
        _print_ops(getattr(a, "ops_results", None))
    return rc


def cmd_list(a) -> int:
    src = _expand([a.input])[0]
    sel = {k: getattr(a, k) for k in ("name", "layer", "material", "definition") if getattr(a, k)}
    ops = [{"op": "summary"}, {"op": "list", "select": sel, "limit": a.limit}]
    a.no_textures, a.keep_triangles, a.width, a.height = True, False, 800, 600
    a.allow_external = getattr(a, "allow_external", False)
    step = _Progress(a.quiet or a.json)
    with tempfile.TemporaryDirectory(prefix="skptool_") as tmp:
        tmp = Path(tmp)
        ops_path = tmp / "ops.json"
        ops_path.write_text(json.dumps(ops), encoding="utf-8")
        try:
            if src.suffix.lower() == ".skp":
                stats = _skp_into_blender(src, [], a, tmp, step, ops_path)
            else:
                step(f"Blender liest {src.name}")
                stats = run_bridge(["load", "--in", str(src.resolve()), "--ops", str(ops_path)],
                                   blender=a.blender, verbose=a.verbose)["stats"]
        except (BlenderError, Exception) as exc:  # noqa: B014
            print(f"FEHLER {src.name}: {_explain(src, exc, a.verbose)}", file=sys.stderr)
            return 1
    summary, listing = stats["ops"][0], stats["ops"][1]
    if a.json:
        print(json.dumps({"summary": summary, "list": listing}, indent=2, ensure_ascii=False))
        return 0
    print(f"{src.name}: {summary['objects']} Objekte ({summary['unique_meshes']} eindeutige Geometrien), "
          f"Groesse {summary['size'][0]} x {summary['size'][1]} x {summary['size'][2]} m")
    print("Ebenen: " + ", ".join(f"{l['name']} ({l['objects']})" + (" [aus]" if l["hidden"] else "")
                                 for l in summary["layers"]))
    print(f"{listing['count']} Objekte passen" + (f" zu {sel}" if sel else "") + ":")
    for o in listing["objects"]:
        mats = ", ".join(o["materials"][:3]) + (" ..." if len(o["materials"]) > 3 else "")
        print(f"  {o['name'][:40]:40} {o['layer'][:18]:18} {o['size'][0]:>7.2f} x {o['size'][1]:>6.2f} x "
              f"{o['size'][2]:>6.2f} m  {mats}")
    if listing["truncated"]:
        print(f"  ... gekuerzt, mehr mit --limit")
    return 0


def cmd_render(a) -> int:
    a.output = a.output or str(Path(a.input).with_suffix(".png"))
    a.format = None
    a.outdir = None
    a.inputs = [a.input]
    return cmd_convert(a)


def _check_before_window(src: Path, a) -> int:
    """Eine fremde .blend vor dem Oeffnen im Fenster pruefen: Netzwerkpfade nie, externe Dateien
    nur mit --allow-external (im Fenster laedt Blender sie sofort und ohne unsere Filter)."""
    try:
        res = run_bridge(["check", "--in", str(src.resolve())] + (["--allow-external"] if a.allow_external else []),
                         blender=a.blender, verbose=a.verbose)
    except BlenderError as exc:
        print(f"FEHLER {src.name}: {_explain(src, exc, a.verbose)}", file=sys.stderr)
        return 1
    found = res["stats"].get("external_files") or []
    if found and not a.allow_external:
        print(f"FEHLER {src.name}: verweist auf {len(found)} externe Datei(en), die Blender beim Oeffnen "
              "laden wuerde:", file=sys.stderr)
        for f in found[:5]:
            print(f"  {f}", file=sys.stderr)
        print(f'Nur bei sicherer Quelle: --allow-external. Oder eine bereinigte Kopie erzeugen: skptool convert '
              f'"{src}" -o "{src.with_name(src.stem + "_bereinigt.blend")}"', file=sys.stderr)
        return 1
    return 0


def _pruefe_vorhandene_blend(src: Path, blend: Path, force: bool) -> str:
    """Eine vorhandene .blend, die neuer ist als die Eingabe, enthaelt vermutlich Aenderungen aus
    Blender: nicht still ueberschreiben. Rueckgabe: Hinweis, welche Datei erzeugt wird."""
    if not blend.exists():
        return f"Erzeuge {blend} aus {src.name}"
    if blend.is_dir():
        raise SystemExit(f"{blend} ist ein Ordner. Nichts geschrieben.")
    if blend.stat().st_mtime < src.stat().st_mtime:
        return f"Erzeuge {blend} neu aus {src.name} (die vorhandene .blend ist aelter als {src.name})"
    if force:
        return f"Erzeuge {blend} neu aus {src.name} (--force: die vorhandene, neuere .blend wird ersetzt)"
    raise SystemExit(f"{blend} gibt es schon und ist neuer als {src.name}. Sie enthaelt vermutlich Aenderungen "
                     f"aus Blender, die beim Neu-Erzeugen verloren gingen. Nichts geschrieben.\n"
                     f'  Die vorhandene Datei oeffnen: skptool open "{blend}"\n'
                     f"  Aus {src.name} neu erzeugen und {blend.name} ueberschreiben: --force")


def cmd_open(a) -> int:
    # Erst alle Pruefungen, dann schreiben: nichts darf eine Datei anlegen oder ueberschreiben,
    # bevor feststeht, dass Blender auch wirklich startet.
    src = _expand([a.input])[0]
    ist_blend = src.suffix.lower() == ".blend"
    if a.export_skp and not a.live:
        raise SystemExit("--export-skp geht nur zusammen mit --live")
    if ist_blend:
        if a.output:
            raise SystemExit("-o geht nur beim Umwandeln einer anderen Datei, eine .blend wird direkt geoeffnet")
        if a.ops:
            raise SystemExit("--ops geht beim Oeffnen nur beim Umwandeln einer anderen Datei. Eine .blend "
                             "bearbeiten: skptool edit oder skptool open ... --live mit skptool live --ops")
        blend, hinweis = src, None
    else:
        blend = Path(a.output) if a.output else src.with_suffix(".blend")
        if blend.suffix.lower() != ".blend":
            raise SystemExit(f"-o muss eine .blend-Datei sein, nicht {blend.name}. Nichts geschrieben.")
        if _same_file(blend, src):
            raise SystemExit(f"Ziel und Quelle sind dieselbe Datei: {src}")
        hinweis = _pruefe_vorhandene_blend(src, blend, getattr(a, "force", False))
    target = None
    if a.live:
        from skptool import live
        target = live.export_target(src, blend, a)
        try:
            live.ensure_free()  # vor der langen Umwandlung pruefen, ob schon eines laeuft
        except live.LiveError as exc:
            print(f"FEHLER: {exc} Nichts geschrieben.", file=sys.stderr)
            return 1
    if ist_blend:
        rc = _check_before_window(src, a)  # das Fenster laedt die Datei ohne unsere Pruefung
        if rc:
            return rc
    else:
        print(hinweis, flush=True)
        a.inputs, a.format, a.outdir = [str(src)], None, None
        a.output = str(blend)
        rc = cmd_convert(a)
        if rc:
            return rc
    if a.live:
        return live.open_live(src, blend, a, target)
    launch_gui(str(blend.resolve()), blender=a.blender)
    print(f"Blender startet mit {blend}. Nach dem Bearbeiten speichern und zurueck mit:")
    print(f'  skptool convert "{blend}" -o "{src.with_name(src.stem + "_bearbeitet.skp")}"')
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--blender", help="Pfad zu blender.exe (sonst automatisch gesucht, "
                                          "oder Umgebungsvariable SKPTOOL_BLENDER)")
    common.add_argument("--no-textures", action="store_true", help="Texturen weglassen")
    common.add_argument("--keep-triangles", action="store_true",
                        help="Beim Import nach Blender Dreiecke nicht zu Flaechen zusammenfassen")
    common.add_argument("--unit-scale", type=float, default=None,
                        help="Beim Schreiben von .skp: Meter pro Blender-Einheit (Standard 1.0)")
    common.add_argument("--width", type=int, default=1600, help="Breite fuer PNG-Vorschau")
    common.add_argument("--height", type=int, default=1000, help="Hoehe fuer PNG-Vorschau")
    common.add_argument("-q", "--quiet", action="store_true", help="Keine Fortschrittsanzeige")
    common.add_argument("--ops", help="Bearbeitungsoperationen als JSON-Text, .json-Datei oder - fuer stdin "
                                      "(siehe edit)")
    common.add_argument("-v", "--verbose", action="store_true",
                        help="Blender-Ausgabe und Zeiten je Schritt anzeigen")
    common.add_argument("--verify", action="store_true",
                        help="Geschriebene .skp immer zur Kontrolle neu einlesen (braucht viel Speicher)")
    common.add_argument("--allow-external", action="store_true",
                        help="Lokale Dateien uebernehmen, auf die eine Eingabe (.blend, .gltf, .obj, .usd ...) "
                             "verweist. Standard: nur eingebettete Daten. Netzwerkpfade nie")
    common.add_argument("--no-verify", action="store_true",
                        help="Kontroll-Einlesen auslassen (Standard: nur bis 100 MB Ausgabegroesse)")

    ap = argparse.ArgumentParser(prog="skptool", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"skptool {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("info", help="Inhalt einer .skp-Datei anzeigen")
    p.add_argument("inputs", nargs="+")
    p.add_argument("--json", action="store_true", help="Ausgabe als JSON")
    p.add_argument("--all", action="store_true", help="Alle Materialien und Komponenten auflisten")
    p.add_argument("--fast", action="store_true", help="Ohne Abmessungen (schneller bei grossen Dateien)")
    p.add_argument("--bounds", action="store_true",
                   help="Abmessungen auch bei Dateien ueber 50 MB berechnen (braucht viel Arbeitsspeicher)")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("convert", parents=[common], help="Konvertieren (skp <-> glb/obj/fbx/blend/...)",
                       description="Ziel .glb .obj .stl .ply .dxf .ifc .json .3mf direkt; .blend .fbx .usd(z) "
                                   ".abc .gltf .png ueber Blender; .skp aus .skp (2017-Format) oder aus "
                                   "jeder Blender-lesbaren Datei.")
    p.add_argument("inputs", nargs="+")
    p.add_argument("-o", "--output", help="Zieldatei (Endung bestimmt das Format)")
    p.add_argument("-f", "--format", help="Zielformat fuer Stapelbetrieb, z. B. glb, fbx, blend, skp")
    p.add_argument("-d", "--outdir", help="Zielordner fuer Stapelbetrieb")
    p.add_argument("--force", action="store_true",
                   help="Stapelbetrieb: vorhandene Zieldateien ueberschreiben")
    p.add_argument("-j", "--jobs", type=stapel.jobs_wert, default=1, metavar="N",
                   help="Stapelbetrieb: bis zu N Dateien gleichzeitig in eigenen Prozessen (0 oder auto: so "
                        "viele wie Kerne). Grosse Dateien laufen allein, damit der Arbeitsspeicher reicht. "
                        "Standard 1")
    p.set_defaults(func=cmd_convert)

    p = sub.add_parser("render", parents=[common], help="PNG-Vorschaubild rendern")
    p.add_argument("input")
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("list", parents=[common], help="Objekte auflisten und filtern (Namen fuer edit)")
    p.add_argument("input")
    p.add_argument("--name", help="Muster fuer Objektnamen, z. B. 'Palme*'")
    p.add_argument("--layer", help="nur Objekte dieser Ebene")
    p.add_argument("--material", help="nur Objekte mit diesem Material")
    p.add_argument("--definition", help="nur Platzierungen dieser Komponente")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("edit", parents=[common], help="Modell per Operationen bearbeiten",
                       description="Operationen als JSON (Text oder .json-Datei): list, summary, measure, move, "
                                   "rotate, scale, mirror, align, distribute, set_material, recolor, set_layer, "
                                   "hide_layer, show_layer, hide, show, delete, rename, duplicate, array, add_box. "
                                   "Beschreibung in skptool/blender_scripts/ops.py.")
    p.add_argument("input")
    p.add_argument("-o", "--output", required=True)
    p.set_defaults(func=cmd_edit)

    p = sub.add_parser("open", parents=[common], help=".blend erzeugen und in Blender oeffnen")
    p.add_argument("input")
    p.add_argument("-o", "--output", help="Pfad der .blend-Datei")
    p.add_argument("--live", action="store_true",
                   help="Blender mit Live-Server starten: skptool live schickt Befehle ins offene Fenster, "
                        "Speichern schreibt automatisch die .skp")
    p.add_argument("--export-skp", metavar="ZIEL",
                   help="Mit --live: diese .skp nach jedem Speichern schreiben "
                        "(Standard bei .skp-Eingabe: <name>_bearbeitet.skp, nie das Original)")
    p.add_argument("--force", action="store_true",
                   help="Eine vorhandene .blend auch dann neu erzeugen, wenn sie neuer ist als die Eingabe "
                        "(Aenderungen darin gehen verloren)")
    p.set_defaults(func=cmd_open)

    from skptool import live
    live.add_live_parser(sub)
    from skptool import bericht, mcp_server, vergleich
    bericht.add_report_parser(sub)
    vergleich.add_diff_parser(sub)
    mcp_server.add_mcp_parser(sub)
    return ap


# Steuerzeichen (ESC usw.), C1-Codes und Bidi-Steuerzeichen sichtbar machen: Namen aus fremden
# Dateien koennten sonst Terminal-Befehle ausloesen (Farben, Titel, Zwischenablage per OSC 52).
_UNSAFE = {c: f"\\x{c:02x}" for c in [*range(0x00, 0x09), 0x0B, 0x0C, *range(0x0E, 0x20), *range(0x7F, 0xA0)]}
_UNSAFE.update({c: f"\\u{c:04x}" for c in [*range(0x202A, 0x202F), *range(0x2066, 0x206A), 0x200E, 0x200F,
                                           0x061C, 0x2028, 0x2029]})
_UNSAFE[0x0D] = "\\r"


class _SafeText:
    """Ausgabestrom, der unsichere Zeichen maskiert (Zeilenumbruch und Tab bleiben)."""

    def __init__(self, inner):
        self._inner = inner

    def write(self, text):
        return self._inner.write(text.translate(_UNSAFE) if isinstance(text, str) else text)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    out, err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = _SafeText(out), _SafeText(err)
    try:
        a = build_parser().parse_args(argv)
        try:
            sys.exit(a.func(a))
        except KeyboardInterrupt:
            print("\nAbgebrochen.", file=sys.stderr)
            sys.exit(130)
    finally:
        sys.stdout, sys.stderr = out, err
