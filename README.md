# skptool

**SketchUp-Dateien (`.skp`) lesen, in offene Formate umwandeln, in Blender bearbeiten und wieder als `.skp` speichern, ohne SketchUp und ohne Trimble-SDK.**

`skptool` ist eine Kommandozeile auf Basis von [OpenSKP](https://github.com/iamahsanmehmood/openskp), einem MIT-lizenzierten, per Reverse Engineering gebauten Leser und Schreiber für `.skp`, und [Blender](https://www.blender.org) als Bearbeitungsprogramm. Es liest Dateien aus SketchUp 2013 bis 2026. Die Recherche zu allen Alternativen steht in [RECHERCHE.md](RECHERCHE.md).

> SketchUp ist eine Marke von Trimble Inc. Dieses Projekt ist nicht mit Trimble verbunden und wird von Trimble weder unterstützt noch geprüft.

| SketchUp-Beispiel, über `skptool` in Blender geladen | Bearbeitet (zwei Stühle dazu, Tisch verschoben) und zurück als `.skp` |
|---|---|
| ![Original](docs/stuhl_original.png) | ![Bearbeitet](docs/stuhl_bearbeitet.png) |

## Was es kann

- **Umwandeln** von `.skp` nach glTF, OBJ, STL, PLY, DXF, IFC, JSON, `.blend`, FBX, USD, Alembic und PNG, und aus allen Blender-lesbaren Formaten zurück nach `.skp`.
- **Rundreise über Blender:** Komponenten, Gruppen in Gruppen, Tags, Materialien, Texturen (vorne und hinten), Deckkraft, harte und weiche Kanten bleiben erhalten.
- **Bearbeiten per Befehl** mit einer festen Liste von Operationen (verschieben, drehen, skalieren, einfärben, kopieren, löschen, ...), ohne Blender-Kenntnisse.
- **Live-Bearbeitung** im offenen Blender-Fenster: Befehle wirken sofort, jedes Speichern schreibt automatisch die `.skp`. Gedacht auch für Skripte und KI-Assistenten.
- **MCP-Server** für KI-Assistenten wie Claude: `skptool mcp` stellt Lesen, Vergleichen, Umwandeln, Bearbeiten und die Live-Steuerung als Werkzeuge bereit.
- **3MF für den 3D-Druck**, direkt ohne Blender.
- **Neue Dateien ins 2017-Format umschreiben**, damit ältere Programme sie öffnen.
- **Für fremde Dateien gebaut:** keine Codeausführung, keine Netzwerkzugriffe, keine fremden lokalen Dateien in der Ausgabe. Details in [SECURITY.md](SECURITY.md).

## Installation

Voraussetzungen: **Python 3.12 oder neuer** und **Blender** (getestet mit 5.2 LTS; Versionen ab 4.2 sollten gehen, sind aber ungetestet). Blender ist nur für die Blender-Formate, Vorschaubilder und den Weg zurück nach `.skp` nötig. Entwickelt und getestet unter Windows, unter macOS und Linux ungetestet.

```bash
python -m venv .venv
```

```bash
.venv\Scripts\python -m pip install --require-hashes --no-deps -r requirements.lock
```

`requirements.lock` enthält Prüfsummen für jedes Paket, so kann bei der Installation nichts untergeschoben werden. Unter macOS und Linux heißt der Pfad `.venv/bin/python`.

Aufruf unter Windows über `skptool.cmd` im Projektordner. Damit `skptool` in jedem Ordner funktioniert, ohne den PATH zu ändern:

```bash
.venv\Scripts\python tools\aufruf_einrichten.py --ja
```

Das legt einen kleinen Starter in `%USERPROFILE%\.local\bin` an (unter macOS und Linux `~/.local/bin/skptool`). Dieser Ordner steht meist schon im PATH, etwa durch uv. Ohne `--ja` zeigt das Skript nur, was es tun würde. `--ziel` wählt einen anderen Ordner, `--entfernen --ja` löscht den Starter wieder, fremde Dateien werden nie überschrieben. Danach ein neues Terminal öffnen und `skptool --version` testen.

Blender wird in den üblichen Installationsordnern gefunden, sonst `SKPTOOL_BLENDER` auf den Pfad setzen oder `--blender` angeben. Aus dem aktuellen Ordner wird Blender nie gestartet.

## Befehle

```bash
skptool info haus.skp
```

Version, Abmessungen, Ebenen, Materialien und Komponenten. `--json` maschinenlesbar, `--all` vollständig.

```bash
skptool convert haus.skp -o haus.blend
```

Die Endung der Zieldatei bestimmt das Format. Stapelbetrieb mit Muster, Format und Zielordner:

```bash
skptool convert "projekte\*.skp" -f glb -d export
```

Vorhandene Zieldateien überschreibt der Stapelbetrieb nur mit `--force`. Mit `--jobs auto` (oder `--jobs N`) laufen mehrere Dateien gleichzeitig, jede in einem eigenen Prozess. Ein Speicherwächter schätzt den Bedarf jeder Datei (grob 100-mal die Dateigröße, mit Blender 0,5 GB mehr) und startet nur so viele Prozesse, dass zusammen höchstens 70 Prozent des freien Arbeitsspeichers belegt sind, große Dateien laufen allein. Strg+C beendet alle Prozesse samt Blender. Das lohnt sich ab etwa einer Sekunde pro Datei (Blender-Ziele, größere Modelle), viele winzige Dateien nach `.glb` sind nacheinander schneller.

```bash
skptool open haus.skp
```

Erzeugt `haus.blend` und öffnet Blender. Nach dem Bearbeiten und Speichern zurück mit `skptool convert haus.blend -o haus_bearbeitet.skp`. Eine vorhandene `.blend` wird vor dem Öffnen im Hintergrund geprüft, weil Blender im Fenster alles lädt, worauf sie verweist: Netzwerkpfade werden abgelehnt, externe Dateien nur mit `--allow-external` zugelassen.

```bash
skptool render haus.skp -o vorschau.png
```

Vorschaubild ohne Fenster.

```bash
skptool report "projekte\*.skp"
```

Sammelbericht über viele Dateien: je Datei Version, gespeicherte und platzierte Flächen, Komponenten, Ebenen, Materialien (mit Textur, getönt, transparent), Texturen (Anzahl, Größe, längste Kante, nur aus dem Bildkopf gelesen) und Einlesezeit, dazu Warnungen, was beim Umwandeln verloren gehen kann (getönte Texturen, Szenen, Bemaßungen, Texte, Schnittebenen, dynamische Komponenten, Materialien gleicher Farbe, sehr große Dateien und Texturen, Version 2019 oder sehr alt). Nicht lesbare Dateien werden eine Fehlerzeile, der Lauf geht weiter, der Rückgabewert ist dann 1. `--json` liefert ein Dokument für alle Dateien, `--csv` eine Tabelle für Excel (Semikolon, mit Schutz gegen eingeschleuste Formeln), `--html` eine eigenständige Seite ohne Skripte. `-o bericht.html` schreibt in eine Datei, ohne Schalter bestimmt die Endung das Format. `--bounds` berechnet auch die Abmessungen, `--rekursiv` durchsucht mit `"projekte\**\*.skp"` auch Unterordner.

```bash
skptool diff haus.skp haus_bearbeitet.skp
```

Vergleicht zwei `.skp`-Dateien: Ebenen, Materialien (Farbe, Deckkraft, Textur), Komponenten und Gruppen sowie jede Platzierung mit ihrer Lage im Raum ("verschoben um 12 mm", "gedreht oder skaliert"). Gruppen werden über ihren Inhalt zugeordnet, weil sich ihre Nummern beim Umschreiben ändern. `--geometrie` vergleicht alle platzierten Punkte, `--texturen` die Texturlage (beide brauchen viel Arbeitsspeicher). `--toleranz` in mm (Standard 0,1), `--nur ebenen,materialien,definitionen,platzierungen`, `--all` vollständig, `--json` maschinenlesbar, `-q` nur Rückgabewert: 0 gleich, 1 verschieden, 2 Fehler. Gedacht zum Prüfen einer Rundreise, etwa `skptool diff haus.skp haus_zurueck.skp --geometrie`.

Weitere Schalter: `-v` zeigt die Zeit jedes Blender-Schritts. `.skp`-Ausgaben werden zur Kontrolle neu eingelesen, standardmäßig bis 100 MB (`--verify` immer, `--no-verify` nie). `--allow-external` übernimmt lokale Dateien, auf die eine fremde Eingabe verweist (siehe [Sicherheit](#sicherheit)).

### Bearbeiten per Befehl

```bash
skptool list haus.skp --name "Stuhl*"
```

Objekte mit Ebene, Größe und Materialien. Filter: `--name` (Muster mit `*`), `--layer`, `--material`, `--definition` (alle Platzierungen einer Komponente), `--json`.

```bash
skptool edit haus.skp -o haus_neu.skp --ops aenderungen.json
```

Wendet eine Liste von Operationen an und schreibt das Ergebnis in jedem Ausgabeformat. `--ops` nimmt eine `.json`-Datei, JSON-Text oder `-` für die Standardeingabe:

```json
[
  {"op": "move", "select": {"name": "Tisch"}, "by": [0, 0.5, 0]},
  {"op": "duplicate", "select": {"name": "Stuhl"}, "offset": [0.7, 0, 0], "count": 2},
  {"op": "set_material", "select": {"name": "Tischplatte"}, "material": "Anthrazit", "color": [55, 58, 62]},
  {"op": "add_box", "name": "Kiste", "size": 0.3, "at": [-1, 0, 0], "layer": "Deko"}
]
```

Operationen: `list`, `summary`, `measure`, `move`, `rotate`, `scale`, `mirror`, `align`, `distribute`, `set_material`, `recolor`, `set_layer`, `hide_layer`, `show_layer`, `hide`, `show`, `delete`, `rename`, `duplicate`, `array`, `add_box`. Einheiten sind Meter und Grad, z zeigt nach oben. Die Box eines Objekts umfasst immer seinen ganzen Inhalt, wie bei einer SketchUp-Gruppe.

```json
[
  {"op": "align", "select": {"name": "Stuhl*"}, "axis": "y", "to": "max", "to_object": {"name": "Tisch"}},
  {"op": "distribute", "select": {"name": "Stuhl*"}, "axis": "x", "gap": 0.2},
  {"op": "array", "select": {"name": "Stuhl"}, "counts": [3, 2, 1], "spacing": [0.6, 0.5, 0]},
  {"op": "mirror", "select": {"name": "Regal"}, "axis": "x"},
  {"op": "hide", "select": {"name": "Deko*"}},
  {"op": "measure", "select": {"name": "Tisch"}, "to_object": {"name": "Sofa"}}
]
```

`align` richtet an `min`, `center` oder `max` der gemeinsamen Box aus oder an einem Bezugsobjekt (`to_object`). `distribute` verteilt nach Mitten oder mit fester Lücke (`gap`). `array` legt verknüpfte Kopien im Raster an. `mirror` spiegelt an der Ebene durch `pivot` (`self`, `group`, `origin` oder `[x, y, z]`), die Geometrie bleibt geteilt. `hide` und `show` blenden einzelne Objekte samt Inhalt aus oder ein, in der `.skp` ist dann die äußere Gruppe verborgen. `measure` ändert nichts und meldet Größe, Mitte und Abstand. Die genaue Beschreibung steht in [`ops.py`](skptool/blender_scripts/ops.py). Keine Operation führt Code aus oder greift auf Dateien zu. Schlägt eine Operation fehl, wird nichts geschrieben.

**Windows PowerShell 5.1:** JSON-Text in Anführungszeichen (`--ops '[...]'`) kommt dort kaputt an, weil PowerShell die inneren Anführungszeichen entfernt. Dort eine `.json`-Datei verwenden oder die Datei übergeben: `Get-Content aenderungen.json -Raw | skptool edit haus.skp -o haus_neu.skp --ops -`. Für Umlaute in Namen vorher `$OutputEncoding = [Text.Encoding]::UTF8` setzen. PowerShell 7, cmd und Bash sind nicht betroffen.

### Live im offenen Blender

```bash
skptool open haus.skp --live
```

Startet Blender mit einer lokalen Verbindung. Befehle wirken sofort im offenen Fenster, jeder Aufruf ist ein eigener Schritt für Strg+Z (unter Windows PowerShell 5.1 `--ops` wie oben als Datei oder mit `-` übergeben):

```bash
skptool live --ops '[{"op": "move", "select": {"name": "Stuhl*"}, "by": [0, 0, 1]}]'
skptool live --screenshot bild.png
skptool live --save --wait
skptool live --status
skptool live --quit
```

Jedes Speichern, per Strg+S oder `live --save`, schreibt im Hintergrund `haus_bearbeitet.skp`. Gibt es die Datei schon, verlangt `skptool` ein ausdrückliches `--export-skp haus_bearbeitet.skp`. Das Original wird nie überschrieben. `--screenshot` zeigt mit `--view model` das ganze Modell (mit `--select "Stuhl*"` nur die Auswahl), mit `viewport` die 3D-Ansicht wie gerade zu sehen und mit `window` das ganze Fenster. `--json` liefert alle Antworten maschinenlesbar. Ein kleiner Befehl dauert etwa 25 ms, auch bei einem Modell mit 632.000 Flächen. Die Verbindung ist nur auf diesem Rechner erreichbar und durch gegenseitige Anmeldung geschützt.

### Für KI-Assistenten (MCP)

```bash
skptool mcp
```

Startet einen MCP-Server (Model Context Protocol) über stdio. Claude Code, Claude Desktop und andere MCP-Clients nutzen skptool dann direkt als Werkzeuge: `skp_info`, `skp_list`, `skp_diff`, `skp_report`, `skp_convert`, `skp_edit` sowie `live_status`, `live_ops`, `live_screenshot` und `live_undo` für ein mit `skptool open datei.skp --live` gestartetes Blender.

Claude Code:

```bash
claude mcp add skptool -- "C:\Pfad\zu\skptool\skptool.cmd" mcp
```

Claude Desktop (`%APPDATA%\Claude\claude_desktop_config.json`):

```json
{"mcpServers": {"skptool": {
  "command": "C:\\Pfad\\zu\\skptool\\.venv\\Scripts\\python.exe",
  "args": ["-P", "-m", "skptool", "mcp"],
  "env": {"PYTHONPATH": "C:\\Pfad\\zu\\skptool"}}}}
```

`--nur-lesen` bietet nur die lesenden Werkzeuge an, `--ordner D:\Projekte` (mehrfach möglich) lässt nur Dateien in diesen Ordnern zu, `--timeout SEKUNDEN` begrenzt jeden Aufruf (Standard 900). Schreibende Werkzeuge überschreiben nie vorhandene Dateien, außer ausdrücklich mit `ueberschreiben`, und nie eine Eingabe. Prüfen ohne Client: `.venv\Scripts\python tools\mcp_testclient.py`.

## Formate

| Richtung | Formate | Weg |
|---|---|---|
| `.skp` nach | `.glb` `.obj` `.stl` `.ply` `.dxf` `.ifc` `.json` `.3mf` | direkt mit OpenSKP, ohne Blender |
| `.skp` nach | `.blend` `.fbx` `.usd` `.usdz` `.abc` `.gltf` `.png` | über Blender im Hintergrund |
| nach `.skp` | aus `.blend` `.glb` `.gltf` `.fbx` `.obj` `.stl` `.ply` `.usd*` `.abc` | über Blender, geschrieben als SketchUp-2017-Datei |
| `.skp` nach `.skp` | jede lesbare Version, auch 2026 | als SketchUp-2017-Datei neu aufgebaut, ohne Blender |

**3MF für den 3D-Druck** (PrusaSlicer, Bambu Studio, Cura): Millimeter, z oben. Jede Komponente steht einmal in der Datei, Platzierungen verweisen darauf, der Slicer sieht ein Objekt aus mehreren Teilen. Farben als Materialfarben, keine Texturen. SketchUp-Rückseiten werden weggelassen, sonst sähe der Slicer doppelte Flächen ohne Volumen. skptool repariert keine Netze: offene Teile stehen als Hinweis auf der Konsole, damit klar ist, warum ein Slicer sich beschwert.

## Sicherheit

`skptool` behandelt jede Eingabe als möglicherweise feindlich. Kurz gefasst:

- Aus Dateien wird nie Code ausgeführt, Blender läuft immer mit abgeschalteter Skriptausführung.
- Dateien mit Netzwerkpfaden werden abgelehnt, bevor Blender sie öffnet. Unter Windows würde schon der Zugriff Anmeldedaten an einen fremden Server senden.
- Aus fremden Dateien werden standardmäßig nur eingebettete Daten übernommen. Verweist eine Datei auf Bilder, Bibliotheken, Caches oder andere Dateien auf diesem Rechner, werden diese nicht verwendet und `skptool` sagt, welche. Nur für Dateien aus sicherer Quelle: `--allow-external`.
- Zip-Bomben, riesige Texturen, explodierende Verschachtelungen und Endlosjobs werden begrenzt.
- Die Eingabe wird nie überschrieben, Ausgaben werden atomar geschrieben.
- Namen aus Dateien können das Terminal nicht steuern.

Alle Schutzmaßnahmen, der Live-Modus und die bekannten Grenzen stehen in [SECURITY.md](SECURITY.md). Sicherheitslücken bitte vertraulich melden, wie dort beschrieben.

## Was erhalten bleibt

**Von SketchUp nach Blender**

- Geometrie in Metern. Dreiecke werden wieder zu SketchUp-artigen Flächen zusammengefasst.
- Materialnamen, Farben, Texturen und Transparenz.
- Komponenten bleiben Komponenten: Jede Geometrie steht nur einmal im Speicher, jede Platzierung ist eine verknüpfte Kopie. Die Gruppenhierarchie bleibt als Eltern-Kind-Beziehung erhalten, Objekte heißen wie in SketchUp.
- Ebenen (Tags) als Blender-Collections, ausgeblendete bleiben ausgeblendet, auch bei Dateien vor 2021.
- Auf ganze Gruppen gemalte Materialien werden an deren unbemalte Flächen vererbt, wie in SketchUp.
- Vorder- und Rückseite einer Fläche werden eine Fläche, die Rückseitenfarbe bleibt für den Rückweg gespeichert.
- Rundungen sind glatt schattiert, harte SketchUp-Kanten bleiben scharf markiert.
- Unbemalte Flächen bekommen das Material `SketchUp_Standard` und werden beim Rückweg wieder unbemalt.

**Von Blender nach SketchUp**

- Die Verschachtelung bleibt erhalten: Aus der Hierarchie in Blender werden wieder Gruppen in Gruppen und Komponenten in Komponenten. Beim mitgelieferten Beispiel ist der Baum nach der Rundreise identisch mit dem Original.
- Gleiche Teilbäume werden eine Komponente mit mehreren Platzierungen, auch Kopien, die erst in Blender entstanden sind. Wird eine Kopie geändert, bekommt sie eine eigene Definition ("Stuhl#2"), wie "Eindeutig machen" in SketchUp.
- Drehung, Spiegelung, ungleichmäßige Skalierung und Scherung jeder Platzierung werden exakt übertragen.
- Materialien mit Farbe, Deckkraft und Bildtexturen auf Vorder- und Rückseite. Die Texturlage wird so geschrieben, wie SketchUp sie liest, auch perspektivisch verzerrte Texturen. Bei den Testmodellen haben danach 98,6 bis 99,3 Prozent aller texturierten Ecken exakt dieselbe Texturlage wie im Original.
- Nicht ebene, konkave und gelochte Flächen werden sauber trianguliert.
- Nur harte Kanten sind in SketchUp als Linie sichtbar, Rundungen und Dreiecksdiagonalen werden weich.
- Texturmaterialien bekommen die Durchschnittsfarbe ihres Bildes (Materialbrowser, Stile ohne Texturen).
- Jede Collection wird ein Tag, auch an übergeordneten Gruppen. Leere Ebenen bleiben erhalten.

## Grenzen

- **Ausgabe immer im 2017-Format.** OpenSKP kann nur dieses Format schreiben. Aktuelle SketchUp-Versionen öffnen solche Dateien, geprüft in SketchUp Free (Web).
- **Gruppe oder Komponente** erkennt `skptool` am Namen der Definition, weil OpenSKP die Unterscheidung beim Lesen nicht liefert: "Group#12", "Gruppieren#3" usw. werden Gruppen, alles mit eigenem Namen wird eine Komponente.
- **Bemalung ganzer Gruppen** wird an die Flächen darin vererbt und dort zurückgeschrieben. Verschieden bemalte Kopien einer Komponente werden deshalb eigene Definitionen, das Aussehen stimmt.
- **Nicht jede ältere Datei ist lesbar.** OpenSKP scheitert laut Projekt an manchen Dateien aus SketchUp 2019 und sehr alten Versionen. Version 7 und älter liest erst OpenSKP 1.3.0.
- **Große Dateien brauchen beim Einlesen viel Arbeitsspeicher**, bei 200 MB gut 12 GB. Der Rückweg bleibt unter 1 GB. `info` berechnet die Abmessungen ab 50 MB nur mit `--bounds`.
- **Getönte Texturen** (SketchUp "Colorize") verlieren beim Umschreiben ihre Tönung.
- **Szenen, Bemaßungen, Texte, Schnittebenen und dynamische Komponenten** werden nicht übertragen.
- **Stark verzerrte Texturen auf gekrümmten Flächen** können leicht verrutschen.
- **Materialien gleicher Farbe** können verwechselt werden, weil OpenSKP Materialien für die Szene nur über ihre Farbe unterscheidet.

## Geschwindigkeit

Gemessen mit Blender 5.2 unter Windows an zwei nicht öffentlichen Projektdateien aus SketchUp 2026:

| Datei | Schritt | Zeit | Speicherspitze |
|---|---|---|---|
| 22 MB, 239.000 Flächen | `.skp` nach `.blend` | 34 bis 47 s | 2,3 GB |
| 22 MB | `.blend` nach `.skp` | 15 bis 37 s (mit Kontroll-Einlesen) | unter 1 GB |
| 201 MB, 1,2 Mio. Flächen | `.skp` nach `.blend` | 210 bis 275 s | 12,4 GB |
| 201 MB | `.blend` nach `.skp` | 45 bis 70 s | unter 1 GB |

Die Zeit steckt fast vollständig im Einlesen durch OpenSKP in Python. Die Optimierungen dahinter beschreibt [RECHERCHE.md](RECHERCHE.md).

## Wie geprüft wurde

- **Automatische Tests**, siehe unten, darunter eine Rundreise mit Texturtestszene, gescherten Platzierungen und verschachtelten Gruppen.
- **Echte Projektdateien** (22 MB und 201 MB, nicht öffentlich): Nach der kompletten Rundreise stimmen alle platzierten Punkte auf 0,1 mm mit der vorherigen, in SketchUp geprüften Fassung überein, und 99,1 bis 99,3 Prozent der texturierten Ecken haben exakt dieselbe Texturlage wie im Original.
- **In SketchUp Free (Web)** wurden erzeugte Dateien in mehreren Runden geöffnet und visuell geprüft, darunter eine Texturtestszene mit gedrehten, schrägen und gekachelten Flächen.
- **Zweiter Leser:** Der unabhängige Rust-Leser Hew/openskp öffnet die erzeugten `.skp` mit den richtigen Definitionen und Tags. Bei großen Dateien meldet er einzelne Resync-Stellen; er wertet nach eigener Angabe nur Teile einer Datei aus.
- **Sicherheitsaudit** vor der Veröffentlichung, mit einem Test für jeden Befund (`tests/test_sicherheit.py`).

## Tests

```bash
.venv\Scripts\python -m unittest discover -s tests -v
```

223 Tests in `tests/`: Grundfunktionen, Sicherheit, Live-Modus, MCP-Server, `diff`, `report`, 3MF, Stapelbetrieb, Bearbeitungsoperationen, Rundreise-Geometrie, Kanten, Aufruf von überall, OpenSKP-Vertrag und Stil.

Einige Tests brauchen zwei zusätzliche Beispieldateien aus dem OpenSKP-Repository. Sie enthalten Inhalte Dritter und liegen deshalb nicht bei. Laden (fester Stand, per SHA-256 geprüft):

```bash
.venv\Scripts\python tools\beispiele_laden.py
```

Ohne sie werden diese Tests übersprungen.

## Aufbau

| Datei | Aufgabe |
|---|---|
| `skptool/cli.py` | Befehle und Ausgabe |
| `skptool/core.py` | alles, was OpenSKP direkt macht: Analyse, Export, Umschreiben, Schreiben aus Blender-Daten, Triangulierung |
| `skptool/gltf_writer.py` | instanzerhaltende GLB mit numpy, samt Ebenen, Bemalung und exakter Zerlegung gescherter Matrizen |
| `skptool/blender.py` | Blender finden und im Hintergrund starten |
| `skptool/blender_scripts/bridge.py` | läuft in Blender: Import, Export, Vorschau, Datenexport für den Rückweg |
| `skptool/blender_scripts/refcheck.py` | prüft fremde Dateien auf Netzwerk- und externe Verweise |
| `skptool/blender_scripts/ops.py` | Bearbeitungsoperationen für `edit` und den Live-Modus |
| `skptool/live.py`, `skptool/blender_scripts/live_server.py` | die beiden Seiten des Live-Modus |
| `skptool/opsjson.py` | liest und prüft `--ops` |
| `skptool/vergleich.py` | `skptool diff`, auch Grundlage für den Geometrie- und Texturvergleich in `tools/` |
| `skptool/bericht.py` | `skptool report` |
| `skptool/mcp_server.py` | `skptool mcp`, der MCP-Server für KI-Assistenten |
| `skptool/export_3mf.py` | 3MF-Export |
| `tools/` | Hilfsskripte: Aufruf von überall einrichten, Texturvergleich, Geometrievergleich, Verschachtelung als Baum, Texturtestszene, Beispiele laden |

## Versionen

Alle Abhängigkeiten sind in `requirements.txt` fest gepinnt, auf Versionen, die bei der Erstellung mindestens 14 Tage veröffentlicht waren. `requirements.lock` enthält dazu die Prüfsummen. OpenSKP ist bewusst auf 1.2.0 gepinnt, weil `skptool` interne Funktionen nutzt. Vor einem Upgrade müssen alle Tests laufen. Änderungen stehen in [CHANGELOG.md](CHANGELOG.md).

## Lizenz

MIT, siehe [LICENSE](LICENSE). Herkunft und Lizenzen der Beispieldateien und Abhängigkeiten stehen in [THIRD_PARTY.md](THIRD_PARTY.md).
