# Änderungen

Format nach [Keep a Changelog](https://keepachangelog.com/de/1.1.0/), Versionen nach [Semantic Versioning](https://semver.org/lang/de/).

## [Unveröffentlicht]

### Hinzugefügt

- **Verschachtelung beim Rückweg.** Aus der Eltern-Kind-Hierarchie in Blender werden wieder Gruppen in Gruppen und Komponenten in Komponenten. Gleiche Teilbäume teilen sich eine Definition, auch Kopien, die erst in Blender entstanden sind. Wird eine Kopie geändert, bekommt sie eine eigene Definition ("Chair#2"). Benannte SketchUp-Komponenten bleiben Komponenten, Definitionen wie "Group#12" werden wieder Gruppen. Tags an übergeordneten Gruppen bleiben erhalten.
- **Live-Modus.** `skptool open datei.skp --live` startet Blender mit einer lokalen Verbindung. `skptool live --ops`, `--screenshot`, `--save`, `--undo`, `--status` und `--quit` steuern das offene Fenster. Jeder Aufruf ist ein eigener Rückgängig-Schritt, jedes Speichern schreibt automatisch `<name>_bearbeitet.skp`. Mit `--json` sind alle Antworten maschinenlesbar.
- **Bearbeiten per Befehl.** `skptool list` zeigt Objekte mit Ebene, Größe und Materialien, `skptool edit` wendet eine JSON-Liste fester Operationen an: `move`, `rotate`, `scale`, `set_material`, `recolor`, `set_layer`, `hide_layer`, `show_layer`, `delete`, `rename`, `duplicate`, `add_box`, `list`, `summary`. Schlägt eine Operation fehl, wird nichts geschrieben.
- `--allow-external` für Eingaben, deren Verweise auf lokale Dateien übernommen werden sollen (siehe Sicherheit).
- `--force` für den Stapelbetrieb.
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
