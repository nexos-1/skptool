# Änderungen

Format nach [Keep a Changelog](https://keepachangelog.com/de/1.1.0/), Versionen nach [Semantic Versioning](https://semver.org/lang/de/).

## [Unveröffentlicht]

### Noch offen (geplant für die nächsten Versionen)

- **Noch nicht in echten Programmen geprüft:** der MCP-Server in den Apps Claude Desktop und Claude Code selbst (geprüft mit MCP Inspector und offiziellem Python-SDK), der parallele Stapelbetrieb unter macOS mit echten Dateien.
- **Noch nicht in SketchUp geprüft:** ob SketchUp die Tönung getönter Texturen aus dem 2017-Format anzeigt, ob Texte und Bemaßungen in Komponenten dort richtig erscheinen, die Lage von Texturen auf nach unten zeigenden Flächen und ausgeblendete Ebenen aus Dateien ab SketchUp 2021.
- **Sehr alte Dateien** (SketchUp 3 bis 8) sind mit `skptool` mangels Testdateien nicht geprüft.
- **Live-Tests unter macOS:** Die Tests mit Blender-Fenster laufen unter Windows und Linux (Xvfb, auch in der CI), unter macOS nicht. Die Zuordnung der selbst gestarteten Blender-Prozesse über `ps` ist dort nur mit einem Parser-Test geprüft (unter Linux liest sie `/proc`, gegengeprüft mit `ps`).
- **3MF in OrcaSlicer:** Teile mit mehreren Farben erscheinen in ihrer Hauptfarbe; Orcas Bemalung je Fläche wäre nicht spezifikationsgemäß.

## [0.3.1] - 2026-09-24

### Hinzugefügt

- **3MF: Farben im Slicer.** Jede Farbe des Modells wird ein Filament (höchstens 16), PrusaSlicer bekommt sie als Bemalung je Fläche, OrcaSlicer je Teil; Texturen mit ihrer Durchschnittsfarbe. Beim Öffnen als Projekt zeigen PrusaSlicer 2.9.6 und OrcaSlicer 2.4.2 die SketchUp-Farben. Die Datei bleibt gültig nach dem XSD der 3MF-Spezifikation und in lib3mf (strenger Modus).
- **Live-Modus unter Linux geprüft:** Die Tests mit echtem Blender-Fenster laufen jetzt auch unter Linux auf einem virtuellen Bildschirm (Xvfb), in der CI als eigener Job und in `tools/linux_e2e.sh` (neu: `--nur-live`).

### Geändert

- Live-Modus: Nur noch Befehle, die etwas ändern, lassen das Blender-Fenster neu zeichnen. Lesende Anfragen wie `--status` warten dadurch nicht mehr auf das Neuzeichnen (mit Software-OpenGL vorher 80 ms, jetzt 20 ms).

### Behoben

- **Blender-Erweiterung:** Wiederholter Import in dieselbe Szene legt keine „Layer0.001", „Walnut.001" oder „Bein.001" mehr an: vorhandene Tags, gleich aussehende Materialien und gleiche Geometrie werden weiterbenutzt. Beim Export werden Collections „X.001" zum Tag „X", bitgleiche Kopien „X.001" zur selben Komponente; anders aussehende Materialien behalten einen eigenen Namen („X_2").

## [0.3.0] - 2026-09-24

### Hinzugefügt

- **Blender-Erweiterung** `skptool_io`: File > Import und File > Export > SketchUp (.skp) in Blender 4.2 oder neuer, über das installierte skptool (ohne OpenSKP in der Erweiterung). Import über `.blend` (Tags, Flächen, harte Kanten bleiben), Export der Szene oder Auswahl, Modifikatoren wahlweise, Fortschritt in der Statusleiste, Esc bricht ab. Bauen und prüfen mit `tools/extension_bauen.py`.
- **Installierbar als Paket** (`pyproject.toml`): `pipx install git+https://github.com/nexos-1/skptool` oder `uv tool install ...` ergibt den Befehl `skptool`. CI baut das Wheel, installiert es sauber und prüft den Befehl.
- `tools/linux_e2e.sh`: Linux-Prüfung mit echten Dateien in Docker (Python 3.12, Blender 5.2.0, Projektordner nur lesend).
- **Englische Doku:** README.en.md und SECURITY.en.md mit Sprachumschalter. `tests/test_doku.py` prüft Schalter, Beispiele, Links und Anker der Doku gegen den echten Parser.
- `skptool report --csv-international`: CSV nach RFC 4180 (Komma, Dezimalpunkt) für Excel mit englischer Spracheinstellung und für Skripte.
- `skptool open --force` erzeugt eine vorhandene `.blend` neu, auch wenn sie neuer ist als die `.skp`.
- **MCP-Server:** `skp_convert` schreibt `.3mf` (nur aus `.skp`, ohne Blender). JSON-RPC-Batches, wenn Protokoll 2025-03-26 ausgehandelt ist (diese Version verlangt sie). Mit echten Clients geprüft: MCP Inspector 2.6.0 und offizielles Python-SDK 2.2.0, dazu die offiziellen JSON-Schemas je Protokollversion (`tools/mcp_sdk_pruefung.py`).

### Geändert

- **OpenSKP 1.3.0** (neue Abhängigkeit `mapbox-earcut` 2.1.0), als einzige Ausnahme von der 14-Tage-Regel nach Prüfung von Paket und Quellcode. Einlesen großer Dateien braucht deutlich weniger Arbeitsspeicher (Testmodell: 323 statt 538 MB).
- Ausgeblendete Ebenen aus Dateien ab SketchUp 2021 bleiben ausgeblendet.
- Beim Umschreiben ins 2017-Format bleiben die echte Kachelgröße der Texturen und das Verhalten „immer zur Kamera drehen" / „Schatten zur Sonne" erhalten.
- Texturen auf nach unten zeigenden Flächen liegen wie in SketchUp (laut OpenSKP gemessen, vorher um 180 Grad gedreht).
- IFC-Export in Millimetern mit Z nach oben (vorher Zoll-Werte unter der Einheit Millimeter, Modell lag auf der Seite); lose Kanten kommen als Anmerkungen mit.
- `skptool report` nennt bei sehr alten Dateien den Stand von OpenSKP 1.3.0 (SketchUp 7 und 8 laut Projekt lesbar, bei 3, 4 und 6 Lesefehler bekannt).
- **Umschreiben ins 2017-Format:** Frei gesetzte Texte und Bemaßungen bleiben erhalten, auch in Komponenten. Alle Attribut-Wörterbücher der Platzierungen kommen mit ihren Typen an, bisher nur `dynamic_attributes` als Text. Was nicht übertragen werden kann (Szenen, Schnittebenen, verankerte Texte, Formeln dynamischer Komponenten), meldet `convert` je Art in einer Zeile mit Anzahl, `skptool report` sagt dasselbe.
- `skptool report` warnt nicht mehr vor Materialien gleicher Farbe, und bei getönten Texturen nur noch davor, dass die Art der Tönung verloren geht.
- **MCP-Server:** Bei einer unbekannten Protokollversion bietet der Server jetzt seine neueste an (2025-11-25) statt 2025-06-18.

### Behoben

- **Texturen nicht mehr halbdurchsichtig:** Texturen aus Dateien, die skptool oder OpenSKP geschrieben hat, kamen in der `.glb` und in Blender als leicht durchsichtig an (Deckkraft 0,996, in Blender wie Glas sortiert) und über Blender mit dieser Deckkraft zurück; durchsichtige Texturen verloren etwas Deckkraft (0,5 wurde 0,498). Ursache war der Platzhalter der Durchschnittsfarbe, den OpenSKP als Deckkraft las.
- **Doppelte interne IDs:** Ins 2017-Format geschriebene Dateien enthielten mit OpenSKP 1.2.0 je nach Modell 2 bis 45 doppelte persistente IDs, die laut OpenSKP dazu führen können, dass SketchUp die Datei nicht speichern kann. Mit 1.3.0 kommen alle IDs aus einem Zähler, auch die von Texten und Bemaßungen (geprüft in `tests/test_anmerkungen.py`).
- **Materialien gleicher Farbe** werden nicht mehr verwechselt: jede Fläche behält ihr SketchUp-Material, auch über Blender.
- **Getönte Texturen** (SketchUp "Einfärben") behalten beim Umschreiben und auf dem Weg über Blender ihre Tönung. Die `.glb` enthält das nach SketchUps Verfahren getönte Bild.
- Texturen in der `.glb` sind nicht mehr zu dunkel. Durchscheinende Texturen bleiben durchscheinend, Flächen mit nur hinten bemalter Seite drehen sich über Blender nicht mehr um, unbenutzte Farbmaterialien bleiben im Modell.
- **`skptool diff`** paart gleiche Teile jetzt von oben nach unten und optimal: Ein verschobener Tisch erscheint als ein Eintrag, nicht mehr als Beine mit unsinnigen Wegen. „verschoben um" misst relativ zur übergeordneten Gruppe (die Matrizen im JSON bleiben Weltlagen), eine fehlende Gruppe steht als ein Eintrag da.
- **`skptool open`** prüft alles, bevor es schreibt: Ist die `.blend` neuer als die `.skp`, bricht es ab (neu: `--force`). Bisher wurde die `.blend` still überschrieben, auch wenn danach eine Prüfung scheiterte.
- **3MF:** Gedrehte oder skalierte Platzierungen derselben Komponente lagen in OrcaSlicer und Bambu Studio neben der richtigen Stelle (bis über einen Meter). Netze liegen jetzt um ihre Mitte, geprüft in PrusaSlicer 2.9.6, OrcaSlicer 2.4.2, lib3mf und gegen das XSD der 3MF-Spezifikation.
- **`report --csv`:** Namen wie „1-2", „0012" oder „12:30" kommen in Excel nicht mehr als Datum, Zahl oder Uhrzeit an. Geprüft in Excel 16, LibreOffice 26.2 und (HTML) Edge 153.
- `convert --unit-scale` rechnete mit dem Kehrwert: `--unit-scale 1.0` ergab 1 Zoll statt 1 Meter pro Blender-Einheit.
- **Stapelbetrieb unter Linux in Containern:** Der Speicherwächter beachtet jetzt die Speichergrenze der cgroup (Docker `--memory`, Kubernetes, systemd). Bisher plante `--jobs auto` nach dem Speicher des ganzen Rechners.
- **MCP-Server:** Blender wird auch gefunden, wenn der Client ohne `ProgramFiles` startet (so das Python-SDK): fehlende Ordner-Variablen kommen aus Windows selbst. Die Fehlerantwort auf eine ungültige Anfrage trägt deren id, wenn sie erkennbar ist.
- Live-Tests zählen beim Aufräumen nur noch selbst gestartete Blender-Prozesse und messen die Latenz getrennt nach Zeit in Blender und Abholen durch den Timer; parallel laufende Blender oder Last auf dem Rechner färben sie nicht mehr falsch rot.
- README korrigiert: Überschreiben ohne `--force` nur bei mehreren Dateien, Box bei `list`, `diff --nur modell`, fehlende Schalter ergänzt.

### Sicherheit

- **Fuzzing** aller Eingänge (`tools/fuzz.py`, gut 15.000 Fälle): PLY mit gefälschtem Kopf ließ Blender 9 bis 12 GB reservieren (jetzt vorher abgelehnt), die Versionserkennung las ganze Dateien, der Live-Client ließ sich durch tropfenweises Senden festhalten, `--ops` nahm `1e400` als Unendlich an, kaputte ZIP-Versionen und manche Fehler kamen als englische Python-Meldung oder über zwei Zeilen.
- **Blender-Erweiterung** nur mit der Berechtigung „files", ohne Netzwerk. skptool startet sie nur über den Pfad aus den Add-on-Einstellungen, ohne Shell und nie über `.cmd`/`.bat`.
- Unsinnige Kachelgrößen und ungültige Texturpunkte (NaN) aus präparierten Dateien werden beim Umschreiben abgefangen.
- `mapbox-earcut` (kompilierte Erweiterung, neu mit OpenSKP 1.3.0) wird nur aus dem Lock mit Prüfsummen installiert. Das Paket wird per Trusted Publishing aus dem GitHub-Projekt der Bindung veröffentlicht, sein `earcut.hpp` entspricht mapbox/earcut.hpp.
- Bei Installation mit pipx oder uv tool lädt `skptool` keine Module aus dem aktuellen Ordner, obwohl der Starter der Paketverwaltung Python ohne `-P` startet (`tests/test_paket.py`).

## [0.2.0] - 2026-09-23

### Hinzugefügt

- **Verschachtelung beim Rückweg.** Aus der Eltern-Kind-Hierarchie in Blender werden wieder Gruppen in Gruppen und Komponenten in Komponenten. Gleiche Teilbäume teilen sich eine Definition, auch Kopien, die erst in Blender entstanden sind. Wird eine Kopie geändert, bekommt sie eine eigene Definition ("Chair#2"). Benannte SketchUp-Komponenten bleiben Komponenten, Definitionen wie "Group#12" werden wieder Gruppen. Tags an übergeordneten Gruppen bleiben erhalten.
- **Live-Modus.** `skptool open datei.skp --live` startet Blender mit einer lokalen Verbindung. `skptool live --ops`, `--screenshot`, `--save`, `--undo`, `--status` und `--quit` steuern das offene Fenster. Jeder Aufruf ist ein eigener Rückgängig-Schritt, jedes Speichern schreibt automatisch `<name>_bearbeitet.skp`. Mit `--json` sind alle Antworten maschinenlesbar.
- **Bearbeiten per Befehl.** `skptool list` zeigt Objekte mit Ebene, Größe und Materialien, `skptool edit` wendet eine JSON-Liste fester Operationen an: `move`, `rotate`, `scale`, `set_material`, `recolor`, `set_layer`, `hide_layer`, `show_layer`, `delete`, `rename`, `duplicate`, `add_box`, `list`, `summary`. Schlägt eine Operation fehl, wird nichts geschrieben.
- `--allow-external` für Eingaben, deren Verweise auf lokale Dateien übernommen werden sollen (siehe Sicherheit).
- `--force` für den Stapelbetrieb.
- **MCP-Server** `skptool mcp` für KI-Assistenten: 10 Werkzeuge über stdio, ohne neue Abhängigkeit, jeder Aufruf in eigenem Prozess mit Zeitlimit, kein Überschreiben, `--nur-lesen` und `--ordner`.
- **3MF-Export** (`-o datei.3mf`) für 3D-Druck-Slicer, ohne Blender und ohne neue Abhängigkeit: Instanzen bleiben erhalten, Millimeter, deterministische Ausgabe, Hinweise auf offene Netze.
- **Neue Operationen** `align`, `distribute`, `array`, `mirror`, `hide`, `show` und `measure`, Screenshot nur einer Auswahl im Live-Modus (`--select`).
- CI-Job mit Blender 5.2.0 auf Linux (per SHA-256 geprüft).
- **Paralleler Stapelbetrieb** `convert --jobs N` oder `--jobs auto`: jede Datei in eigenem Prozess, mit Speicherwächter. Nach `.blend` etwa 2- bis 4-mal, bei größeren Dateien nach `.glb` 4- bis 6-mal schneller.
- `SECURITY.md`, `LICENSE` (MIT), `THIRD_PARTY.md`, `tests/test_sicherheit.py`.
- **`skptool diff`** vergleicht zwei `.skp`-Dateien: Ebenen, Materialien, Komponenten, Gruppen und jede Platzierung, auf Wunsch auch Geometrie und Texturlage. Rückgabewert 0 gleich, 1 verschieden, 2 Fehler.
- **`skptool report`** fasst viele Dateien zusammen, als Text, JSON, CSV (mit Schutz gegen Formeln) oder eigenständiges HTML, mit Warnungen, was beim Umwandeln verloren geht.
- **Aufruf von überall:** `tools/aufruf_einrichten.py` legt einen Starter in `~/.local/bin` an, ohne den PATH zu ändern.
- `--ops -` liest die Operationen von der Standardeingabe (für Windows PowerShell 5.1, dort kommt JSON-Text in Anführungszeichen kaputt an).
- Vertragstests für die genutzten OpenSKP-Interna (`tests/test_openskp_vertrag.py`). Ändert sich OpenSKP, schlagen sie an, statt dass Texturen still falsch liegen. Fehlt eine ersetzte Funktion, bricht `skptool` schon beim Import mit klarer Meldung ab.
- CI auf GitHub (Windows, Linux, macOS), Vorlagen für Fehlermeldungen und Wünsche.
- `tools/baum.py` zeigt die Verschachtelung einer `.skp`, `tools/geometrievergleich.py` vergleicht die platzierte Geometrie zweier Dateien, `tools/beispiele_laden.py` lädt zusätzliche Testdateien.

### Geändert

- **Externe Dateien werden standardmäßig nicht mehr übernommen.** Bisher wurden Bilder und Bibliotheken, auf die eine Eingabe verweist, übernommen, außer mit `--no-external`. Jetzt gilt umgekehrt: nur eingebettete Daten, lokale externe Dateien nur mit `--allow-external`. `--no-external` entfällt.
- Binärer Datenexport aus Blender in Version 4: Elternobjekt, Matrix relativ zum Elternobjekt und Leerobjekte als Container. Dateien der Versionen 1 bis 3 werden weiter gelesen.
- Live-Protokoll Version 2: gegenseitige Anmeldung per HMAC statt Token im Klartext.
- Zwei Beispieldateien mit Inhalten Dritter werden nicht mehr mitgeliefert, sondern bei Bedarf mit `tools/beispiele_laden.py` geladen. Betroffene Tests werden ohne sie übersprungen.
- Vorschaubilder zeigen jetzt das mitgelieferte Stuhl-Beispiel.
- **Umschreiben ins 2017-Format:** Kanten behalten ihre Einstellung hart, weich, glatt und verborgen jetzt je Kante. Vorher galt sie je Fläche, in einer Komponente wurden so aus 966 sichtbaren Kanten 19. Lose Kanten und Gruppen nur aus Linien gehen nicht mehr verloren.
- **Blender-Rundreise behält alle Punkte:** Blender fasste Dreiecke über weiche SketchUp-Kanten hinweg zusammen und löschte dabei innere Punkte, und die Triangulierung von OpenSKP zerlegte konkave Flächen falsch. Jetzt werden nur Dreiecke derselben SketchUp-Fläche zusammengefasst, und schwierige Flächen trianguliert skptool selbst. Bei einem Testmodell gingen vorher 898 von 30.334 Punkten verloren, jetzt keiner.
- **Ausgeblendete Ebenen:** Objekte darauf wurden bisher zusätzlich selbst verborgen geschrieben und blieben in SketchUp auch nach dem Einblenden der Ebene unsichtbar. Jetzt werden ausgeblendete Collections als ausgeblendete Tags geschrieben.
- Nicht unterstützte Zielformate werden abgelehnt, bevor Blender startet. `.dae` als Ausgabe entfällt (Blender 5 enthält kein Collada mehr, der Weg war erreichbar, aber kaputt).
- Unter Linux und macOS gelten `/usr/bin`, `/usr/local/bin`, `/opt`, `/snap/bin` und `/Applications` als geschützte Blender-Installationsorte.
- `tools/geometrievergleich.py` und `tools/texturvergleich.py` brauchen keinen `PYTHONPATH` mehr.

### Sicherheit

Ergebnis eines Audits vor der Veröffentlichung. Zu jedem Punkt gibt es einen Test.

- **Starter gehärtet:** `skptool.cmd` behält aus einem geerbten `PYTHONPATH` nur absolute Pfade (vorher konnten `;.` oder relative Einträge den aktuellen Ordner wieder einschleusen), und `skptool` entfernt beim Import Suchpfade, die auf den aktuellen Ordner zeigen. Unter Windows sucht `skptool` Blender nicht mehr unter `/usr/bin` und ähnlichen Pfaden, die dort als `C:\usr\bin` von jedem angelegt werden könnten.
- **Codeausführung aus dem aktuellen Ordner verhindert.** Unter Windows hätte eine `blender.bat` oder `blender.exe` im aktuellen Ordner Vorrang vor dem installierten Blender gehabt, und `skptool.cmd` hätte Python-Module aus dem aktuellen Ordner geladen (leerer `PYTHONPATH`-Eintrag). Beides ist behoben: Blender wird nur aus Installationsordnern oder absoluten `PATH`-Einträgen gestartet, Python läuft mit `-P` ohne den aktuellen Ordner.
- **Netzwerkpfade** in `.blend`, glTF/GLB, OBJ/MTL, USD, FBX und Alembic werden vor dem Öffnen erkannt und abgelehnt, auch bevor `skptool open` eine `.blend` im Blender-Fenster öffnet. Unter Windows hätte schon der Zugriff den NTLM-Hash an einen fremden Server gesendet.
- **Keine fremden lokalen Dateien in der Ausgabe.** Eine präparierte glTF-Datei konnte über `../`-Pfade beliebige Bilder dieses Rechners in die Ausgabe ziehen, auch mit `--no-external`. Externe Verweise werden jetzt vor und nach dem Laden geprüft und entfernt, einschließlich Caches, Modifikator-Pfaden und Import-Knoten in Geometry Nodes.
- **Live-Modus:** Das Token geht nicht mehr über die Leitung, Antworten werden auf Echtheit geprüft (ein fremder Prozess auf dem Port konnte über die Bildantwort Dateien verschieben lassen). Statusdatei und Ordner unter Linux und macOS nur für den eigenen Benutzer, keine Symlinks. Gesamtfrist je Anfrage gegen langsam sendende Verbindungen, robuste Behandlung beliebiger Eingaben, Exportziel wird nicht still überschrieben.
- **Bearbeitungen begrenzt:** höchstens 1000 Operationen und 100.000 Objekte, nur endliche Zahlen in sinnvollen Grenzen, Namen ohne Steuerzeichen. Vorher konnte `duplicate` den Arbeitsspeicher füllen, und NaN-Werte ergaben kaputte `.skp`-Dateien, die als erfolgreich gemeldet wurden. Der Writer lehnt ungültige Koordinaten jetzt zusätzlich ab.
- **Terminal:** Steuerzeichen und Escape-Sequenzen aus Namen in Dateien werden in jeder Ausgabe maskiert.
- **Dateien:** Der Schutz der Eingabe greift auch bei anderen Schreibweisen desselben Pfads. Der Stapelbetrieb überschreibt keine vorhandenen Dateien und keine eigenen Eingaben mehr. "OK" gibt es nur, wenn die Ausgabe wirklich existiert.
- **Vorschaubilder** enthalten keine Metadaten mehr (vorher unter anderem den Pfad der `.blend`-Datei).
- Texturen werden vor dem Dekodieren auf ihre Größe geprüft, `.usdz`-Container wie `.skp` begrenzt.

## [0.1.0] - 2026-09-23

Erste Version.

### Hinzugefügt

- `skptool info`, `convert`, `render` und `open` für SketchUp-Dateien 2013 bis 2026, ohne SketchUp und ohne Trimble-SDK, auf Basis von OpenSKP 1.2.0 und Blender.
- Direkte Exporte nach `.glb`, `.obj`, `.stl`, `.ply`, `.dxf`, `.ifc` und `.json`, über Blender nach `.blend`, `.fbx`, `.usd`, `.usdz`, `.abc`, `.gltf` und `.png`.
- Rückweg aus jedem Blender-lesbaren Format nach `.skp` (SketchUp 2017), mit Komponenten und Platzierungen, Materialien, Texturen auf Vorder- und Rückseite, Deckkraft, harten und weichen Kanten und allen Tags.
- Instanzerhaltender Import: jede Geometrie nur einmal im Speicher, gescherte Platzierungen exakt übertragen.
- Texturlage wie in SketchUp, auch auf gedrehten, schrägen und perspektivisch verzerrten Flächen. In SketchUp Free (Web) in zwei Runden geprüft.
- Sicherheitsgrenzen für fremde Dateien: Blender ohne Skriptausführung und mit Zeitlimit, Prüfung von ZIP-Containern, Grenzen für Platzierungen und Texturgrößen, atomares Schreiben.
- Hash-gesicherte `requirements.lock`, alle Abhängigkeiten auf Versionen gepinnt, die bei der Erstellung mindestens 14 Tage alt waren.
