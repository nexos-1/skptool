# Sicherheit

## Lücken melden

Bitte Sicherheitslücken **nicht** als öffentliches Issue melden, sondern vertraulich über GitHub: Reiter "Security", dann "Report a vulnerability". Hilfreich sind eine Beispieldatei oder ein Befehl, der das Problem zeigt, und die betroffene Version (`skptool --version`).

## Wovor skptool schützt

`skptool` ist dafür gebaut, auch Dateien aus fremder Hand zu verarbeiten. Diese Schutzmaßnahmen sind eingebaut und mit Tests abgesichert (`tests/test_sicherheit.py`, `tests/test_live.py`):

**Fremde Dateien**

- Aus Dateien wird nie Code ausgeführt. Blender läuft immer mit abgeschalteter Skriptausführung (`-Y`), im Hintergrund zusätzlich mit Werkseinstellungen.
- **Netzwerkpfade** (`\\server\freigabe`, `//server/...`, `file://server/...`) in `.blend`, glTF/GLB, OBJ/MTL, USD, FBX, Alembic und DAE führen immer zur Ablehnung, und zwar bevor Blender die Datei öffnet. Unter Windows würde schon der Zugriff die Anmeldedaten (NTLM-Hash) an den fremden Server senden.
- **Externe Dateien** werden standardmäßig nicht übernommen. Eine fremde Datei könnte sonst beliebige Bilder oder Daten dieses Rechners in eine Ausgabe ziehen, die man weitergibt. Nur mit `--allow-external` werden lokale externe Dateien verwendet. Das betrifft Bilder, verlinkte `.blend`-Bibliotheken, Caches, Schriften, Dateipfade in Modifikatoren und Import-Knoten in Geometry Nodes.
- Auch vor dem Öffnen einer `.blend` im Blender-Fenster (`skptool open`, `--live`) wird geprüft: Netzwerkpfade nie, externe Dateien nur mit `--allow-external`.
- ZIP-Container (`.skp` ab 2021, `.usdz`) werden vor dem Lesen begrenzt: höchstens 8 GB entpackt, 100.000 Einträge, Kompressionsverhältnis 100. Explodierende Verschachtelungen stoppen bei 5 Millionen Platzierungen, übergroße Texturen werden nur anhand ihres Kopfes erkannt und nicht dekodiert.
- Blender-Schritte werden nach einer Stunde abgebrochen (`SKPTOOL_TIMEOUT`).
- Namen aus Dateien können das Terminal nicht steuern: Steuerzeichen, Escape-Sequenzen und Bidi-Zeichen werden in jeder Ausgabe sichtbar maskiert.
- Vorschaubilder enthalten keine Metadaten wie Dateipfade oder Datum.

**Eigene Dateien**

- Die Eingabedatei wird nie überschrieben, auch nicht über andere Schreibweisen desselben Pfads. Im Stapelbetrieb werden vorhandene Dateien nur mit `--force` überschrieben, doppelte Ziele und Ziele, die selbst Eingaben sind, werden abgelehnt.
- Ausgaben werden atomar geschrieben: Scheitert ein Schritt, bleibt eine vorhandene Datei unverändert.

**Programmstart**

- `skptool.cmd` lädt nie Python-Module aus dem aktuellen Ordner, und Blender wird nie aus dem aktuellen Ordner gestartet. Eine `glob.py` oder `blender.bat` neben einem heruntergeladenen Modell bleibt wirkungslos.
- Alle Abhängigkeiten sind mit Prüfsummen gepinnt (`requirements.lock`), auf Versionen, die bei der Erstellung mindestens 14 Tage veröffentlicht waren.

**Bearbeitung (`edit`, `live --ops`)**

- Nur eine feste Liste von Operationen, keine davon führt Code aus oder greift auf Dateien zu.
- Zahlen müssen endlich sein und in sinnvollen Grenzen liegen, Namen sind auf 63 Zeichen ohne Steuerzeichen begrenzt, höchstens 1000 Operationen pro Aufruf und 100.000 Objekte in der Szene.

**Live-Modus**

- Der Server lauscht nur auf `127.0.0.1`. Client und Server weisen sich gegenseitig per HMAC-SHA256 über Zufallswerte aus, das Token selbst geht nie über die Leitung. Ein fremder Prozess auf dem Port erfährt nichts und kann keine gültige Antwort fälschen.
- Webseiten im Browser können keine gültige Anfrage stellen (das Protokoll ist kein HTTP).
- Jede Anfrage hat höchstens 1 MB und 10 Sekunden insgesamt, höchstens 8 Verbindungen gleichzeitig.
- Statusdatei, Ordner und Log sind nur für den eigenen Benutzer lesbar (unter Linux und macOS 0600/0700, Symlinks werden abgelehnt).
- Bilder landen nur im eigenen Temp-Ordner, das Exportziel steht beim Start fest und ist nie die Originaldatei.

## Bekannte Grenzen

- **Blender und OpenSKP lesen die Dateien.** Fehler in deren Lesern (etwa Speicherfehler in einem Importer) liegen außerhalb von `skptool`. Für fremde Dateien gilt deshalb dasselbe wie beim Öffnen in Blender selbst.
- Bei binären FBX- und Alembic-Dateien sucht `skptool` Netzwerkpfade im Dateiinhalt. Ein Pfad, der in komprimierten Blöcken versteckt ist, wird so nicht gefunden. `.blend` (auch komprimiert), glTF, OBJ/MTL und USD werden dagegen vollständig geprüft.
- Unter Linux und macOS ist `skptool` nicht getestet.
