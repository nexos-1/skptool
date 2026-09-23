# Inhalte und Software Dritter

## Mitgelieferte Dateien

| Datei | Herkunft | Lizenz |
|---|---|---|
| `samples/stuhl_tisch_2017.skp` | OpenSKP, `examples/web-viewer/samples/chair_and_table.skp` | MIT, siehe `samples/OPENSKP_LICENSE.txt` |
| `samples/leer_2025.skp` | OpenSKP, `packages/python/tests/fixtures/SU_File.skp` | MIT, siehe `samples/OPENSKP_LICENSE.txt` |

Beide stammen aus [OpenSKP](https://github.com/iamahsanmehmood/openskp), Stand `cb57112da14e8fac5a61e484c5cf21cd08a8791a`, byte-identisch mit dem Original. `docs/stuhl_original.png` und `docs/stuhl_bearbeitet.png` sind mit `skptool` erzeugte Bilder dieses Beispiels.

## Nicht mitgelieferte Testdateien

`tools/beispiele_laden.py` lädt zwei weitere Testdateien aus demselben OpenSKP-Stand nach `samples/extern/`. Sie enthalten Inhalte Dritter, unter anderem ein Automodell mit Herstellerlogo und Namen fremder Personen in Dateipfaden, und werden deshalb nicht mit diesem Projekt verbreitet. Für ihre Nutzung gelten die Bedingungen ihrer Urheber.

## Abhängigkeiten

Nicht Teil dieses Repositorys, installiert über `requirements.lock`:

| Paket | Lizenz |
|---|---|
| openskp 1.2.0 | MIT |
| pillow | MIT-CMU |
| numpy | BSD-3-Clause (enthält weitere freie Lizenzen) |
| trimesh | MIT |
| shapely | BSD-3-Clause (bündelt GEOS unter LGPL-2.1) |
| defusedxml | PSF-2.0 |

[Blender](https://www.blender.org) (GPL) wird separat installiert und nur als eigenes Programm aufgerufen. Die Skripte in `skptool/blender_scripts/` laufen innerhalb von Blender und stehen wie das ganze Projekt unter der MIT-Lizenz, die mit der GPL verträglich ist.

## Marken

SketchUp ist eine Marke von Trimble Inc. Dieses Projekt ist nicht mit Trimble verbunden und wird von Trimble weder unterstützt noch geprüft. Der Name SketchUp dient nur zur Beschreibung des Dateiformats, das `skptool` liest und schreibt.
