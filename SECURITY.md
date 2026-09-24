# Sicherheit

**Deutsch** | [English](SECURITY.en.md)

## Lücken melden

Bitte Sicherheitslücken **nicht** als öffentliches Issue melden, sondern vertraulich über GitHub: Reiter "Security", dann "Report a vulnerability". Hilfreich sind eine Beispieldatei oder ein Befehl, der das Problem zeigt, und die betroffene Version (`skptool --version`).

## Wovor skptool schützt

`skptool` ist dafür gebaut, auch Dateien aus fremder Hand zu verarbeiten. Diese Schutzmaßnahmen sind eingebaut und mit Tests abgesichert (`tests/test_sicherheit.py`, `tests/test_live.py`, `tests/test_fuzz.py`). Alle Eingänge wurden zusätzlich mit gut 15.000 feindlichen Eingaben geprüft (`tools/fuzz.py`, wiederholbar über einen festen Startwert):

**Fremde Dateien**

- Aus Dateien wird nie Code ausgeführt. Blender läuft immer mit abgeschalteter Skriptausführung (`-Y`), im Hintergrund zusätzlich mit Werkseinstellungen.
- **Netzwerkpfade** (`\\server\freigabe`, `//server/...`, `file://server/...`) in `.blend`, glTF/GLB, OBJ/MTL, USD, FBX und Alembic führen immer zur Ablehnung, und zwar bevor Blender die Datei öffnet. Unter Windows würde schon der Zugriff die Anmeldedaten (NTLM-Hash) an den fremden Server senden.
- **Externe Dateien** werden standardmäßig nicht übernommen. Eine fremde Datei könnte sonst beliebige Bilder oder Daten dieses Rechners in eine Ausgabe ziehen, die man weitergibt. Nur mit `--allow-external` werden lokale externe Dateien verwendet. Das betrifft Bilder, verlinkte `.blend`-Bibliotheken, Caches, Schriften, Dateipfade in Modifikatoren und Import-Knoten in Geometry Nodes.
- Auch vor dem Öffnen einer `.blend` im Blender-Fenster (`skptool open`, `--live`) wird geprüft: Netzwerkpfade nie, externe Dateien nur mit `--allow-external`.
- ZIP-Container (`.skp` ab 2021, `.usdz`) werden vor dem Lesen begrenzt: höchstens 8 GB entpackt, 100.000 Einträge, Kompressionsverhältnis 100. Explodierende Verschachtelungen stoppen bei 5 Millionen Platzierungen, übergroße Texturen werden nur anhand ihres Kopfes erkannt und nicht dekodiert.
- PLY-Dateien werden vor dem Laden auf ihre angekündigten Größen geprüft: Ein Kopf, der mehr Punkte oder Flächen ankündigt, als in die Datei passen, wird abgelehnt, bevor Blender dafür Speicher reserviert.
- Vom Dateikopf einer `.skp` werden für die Versionserkennung nur die ersten 200 Byte gelesen, nie die ganze Datei.
- Blender-Schritte werden nach einer Stunde abgebrochen (`SKPTOOL_TIMEOUT`).
- Namen aus Dateien können das Terminal nicht steuern: Steuerzeichen, Escape-Sequenzen und Bidi-Zeichen werden in jeder Ausgabe sichtbar maskiert.
- Jede Fehlermeldung ist eine deutsche Zeile ohne Python-Traceback; technische Einzelheiten zeigt `-v`.
- Vorschaubilder enthalten keine Metadaten wie Dateipfade oder Datum.

**Eigene Dateien**

- Die Eingabedatei wird nie überschrieben, auch nicht über andere Schreibweisen desselben Pfads. Wandelt ein Aufruf mehrere Dateien um, werden vorhandene Zieldateien nur mit `--force` überschrieben, doppelte Ziele und Ziele, die selbst Eingaben sind, werden abgelehnt.
- Ausgaben werden atomar geschrieben: Scheitert ein Schritt, bleibt eine vorhandene Datei unverändert.
- `skptool open` prüft alles, bevor es schreibt, und ersetzt eine `.blend`, die neuer ist als ihre `.skp`, nur mit `--force`.

**Programmstart**

- `skptool.cmd` lädt nie Python-Module aus dem aktuellen Ordner, und Blender wird nie aus dem aktuellen Ordner gestartet. Eine `glob.py` oder `blender.bat` neben einem heruntergeladenen Modell bleibt wirkungslos. Der Starter behält aus einem geerbten `PYTHONPATH` nur absolute Pfade, Python läuft mit `-P`, und `skptool` entfernt beim Import zusätzlich jeden Suchpfad, der auf den aktuellen Ordner zeigt.
- Bei Installation mit pipx, uv tool oder pip startet ein Starter der Paketverwaltung Python ohne `-P`. Auch dann lädt `skptool` keine Module aus dem aktuellen Ordner, weil der Suchpfad beim Ordner des Starters beginnt (geprüft in `tests/test_paket.py`, unter Windows und Linux). Grenze: Enthält `PYTHONPATH` leere oder relative Einträge, lädt Python selbst schon beim Start Code aus dem aktuellen Ordner, bevor `skptool` etwas tun kann. `PYTHONPATH` deshalb nicht setzen oder nur mit absoluten Pfaden; `skptool.cmd` bereinigt das selbst.
- Der Starter aus `tools/aufruf_einrichten.py` ruft nur den `skptool.cmd` dieses Projekts mit absolutem Pfad auf und überschreibt keine fremden Dateien.
- Alle Abhängigkeiten sind auf Versionen gepinnt, die bei der Erstellung mindestens 14 Tage veröffentlicht waren, in `requirements.lock` zusätzlich mit Prüfsummen. Eine Installation mit pipx oder uv tool nutzt dieselben festen Versionen, aber ohne Prüfsummen. Einzige Ausnahme von der 14-Tage-Regel ist OpenSKP 1.3.0, nach Prüfung von Paket und Quellcode freigegeben. `mapbox-earcut` ist eine kompilierte Erweiterung. Sie wird nur aus dem Lock mit Prüfsummen installiert; das Paket wird per Trusted Publishing aus dem GitHub-Projekt der Bindung veröffentlicht.

**Blender-Erweiterung (`skptool_io`)**

- Verlangt nur die Berechtigung „files", kein Netzwerk. skptool wird nur über den Pfad aus den Add-on-Einstellungen gestartet, nie über einen Pfad aus einer `.blend`, immer ohne Shell. `.cmd`/`.bat` werden nie direkt ausgeführt, damit Sonderzeichen in Dateinamen nicht von cmd.exe ausgewertet werden.
- Externe Dateien der Szene werden nur mit „Allow External Files" übernommen.

**Bearbeitung (`edit`, `live --ops`)**

- Nur eine feste Liste von Operationen, keine davon führt Code aus oder greift auf Dateien zu.
- Zahlen müssen endlich sein und in sinnvollen Grenzen liegen, Namen sind auf 63 Zeichen ohne Steuerzeichen begrenzt, höchstens 1000 Operationen pro Aufruf und 100.000 Objekte in der Szene.

**Live-Modus**

- Der Server lauscht nur auf `127.0.0.1`. Client und Server weisen sich gegenseitig per HMAC-SHA256 über Zufallswerte aus, das Token selbst geht nie über die Leitung. Ein fremder Prozess auf dem Port erfährt nichts und kann keine gültige Antwort fälschen.
- Webseiten im Browser können keine gültige Anfrage stellen (das Protokoll ist kein HTTP).
- Jede Anfrage hat höchstens 1 MB und 10 Sekunden insgesamt, höchstens 8 Verbindungen gleichzeitig.
- Auch der Client wartet auf eine Antwort nur begrenzt insgesamt (nicht je Paket): Ein fremder Prozess auf dem Port kann `skptool live` nicht durch tropfenweises Senden festhalten.
- Statusdatei, Ordner und Log sind nur für den eigenen Benutzer lesbar (unter Linux und macOS 0600/0700, Symlinks werden abgelehnt).
- Bilder landen nur im eigenen Temp-Ordner, das Exportziel steht beim Start fest und ist nie die Originaldatei.

**MCP-Server (`skptool mcp`)**

Ein KI-Assistent kann durch Inhalte, die er liest, manipuliert werden (Prompt-Injection). Der Server behandelt deshalb jedes Argument als nicht vertrauenswürdig:

- Ausgaben überschreiben nie eine vorhandene Datei, außer mit `ueberschreiben: true`, und nie eine Eingabe. Das Ergebnis entsteht in einem eigenen Temp-Ordner und wird dann so an seinen Platz gebracht, dass eine inzwischen entstandene Datei nicht ersetzt wird.
- Als Ausgabe nur Formate, die genau eine Datei ergeben (darunter `.3mf`). Eingaben nur mit Endungen, die skptool kennt.
- Netzwerk-, Geräte- und URL-Pfade, Netzlaufwerke, alternative Datenströme (`datei.skp:strom`) und reservierte Gerätenamen werden abgelehnt, bevor darauf zugegriffen wird. Mit `--ordner` nur Dateien in freigegebenen Ordnern.
- Externe Dateien, auf die eine Eingabe verweist, werden nie übernommen, es gibt dafür keinen Schalter.
- Operationen werden wie bei `--ops` geprüft, danach von `ops.py` in Blender. Kein Werkzeug löscht Dateien.
- Jeder Aufruf läuft in einem eigenen Prozess mit Zeitlimit (Standard 900 s), danach werden der Prozess und ein gestartetes Blender beendet. Höchstens 2 Aufrufe arbeiten gleichzeitig.
- Textausgaben sind auf 200 KB begrenzt, Steuer- und Bidi-Zeichen werden maskiert, stdout enthält nur JSON-RPC.
- Grenze: Namen und Texte aus Modellen gelangen als Daten zum Assistenten. Ob er sie als Anweisung missversteht, liegt beim Client. Schreibende Werkzeuge im Client bestätigen lassen oder den Server mit `--nur-lesen` starten.

## Bekannte Grenzen

- **Blender und OpenSKP lesen die Dateien.** Fehler in deren Lesern (etwa Speicherfehler in einem Importer) liegen außerhalb von `skptool`. Für fremde Dateien gilt deshalb dasselbe wie beim Öffnen in Blender selbst.
- Bei binären FBX- und Alembic-Dateien sucht `skptool` Netzwerkpfade im Dateiinhalt. Ein Pfad, der in komprimierten Blöcken versteckt ist, wird so nicht gefunden. `.blend` (auch komprimiert), glTF, OBJ/MTL und USD werden dagegen vollständig geprüft.
- **cmd.exe** sucht Befehle zuerst im aktuellen Ordner. Wer `skptool` in cmd in einem fremden Ordner tippt, bekäme dort eine `skptool.cmd` oder `skptool.bat` zuerst. PowerShell durchsucht den aktuellen Ordner nicht. In cmd hilft die Umgebungsvariable `NoDefaultCurrentDirectoryInExePath=1`.
- Unter Linux laufen alle Tests in der CI auch mit Blender 5.2.0, dazu eine Prüfung mit echten Dateien in Docker (`tools/linux_e2e.sh`), unter macOS ohne Blender. Die Fenster-Tests des Live-Modus laufen unter Windows und unter Linux auf einem virtuellen Bildschirm (Xvfb), unter macOS nicht.
