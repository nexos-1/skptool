# Recherche: SketchUp-Dateien mit offenen Werkzeugen

Stand: 22. September 2026. Fünf parallele Recherchen zu Alternativen, Dateiformat, Blender-Importern, Konvertierungswegen und einer eigenen CLI. Die wichtigsten Aussagen wurden anschließend selbst nachgeprüft, entweder durch Installation und Test oder durch Abruf der Quelle.

## Kurzfassung

- **Ein fertiges Open-Source-SketchUp gibt es nicht, aber seit 2026 erstmals ernsthafte Ansätze.** Hew kommt dem SketchUp-Gefühl am nächsten, liest aber nur Dateien im 2017-Format. IngeTrazo liest neuere Dateien, ist aber noch sehr früh.
- **Der eigentliche Durchbruch ist OpenSKP.** Die MIT-lizenzierte Bibliothek liest SketchUp-Dateien von 2013 bis 2026 ohne SketchUp und ohne Trimble-SDK und kann `.skp` auch schreiben. Hier getestet: Dateien aus 2017, 2020, 2025 und 2026 werden gelesen.
- **Blender ist die beste Bearbeitungsumgebung,** braucht für `.skp` aber ein Add-on. Das verbreitete RedHalo-Add-on importiert nach den ausgewerteten Quellen, exportiert aber nicht.
- **SketchUp Free (Web) exportiert nur SKP, PNG und STL** und kann nicht in ältere Versionen speichern. STL verliert Materialien und Struktur.
- **Das offizielle Trimble-SDK passt nicht zu einem offenen Werkzeug.** Seine Lizenzbedingungen schränken die Weitergabe und die Verwendung in Open-Source-Projekten ein.

## 1. Open-Source- und Gratis-Alternativen

| Programm | Lizenz | Stand | Liest `.skp` | Nähe zu SketchUp |
|---|---|---|---|---|
| Hew | AGPL-3.0 | v1.1.0, 16.09.2026 | ja, nur 2017-Format | sehr hoch: Push/Pull, Follow Me, Maßband, Tags, Szenen |
| IngeTrazo | GPL-3.0 | v0.4.9, 22.09.2026 | laut Projekt 2013 bis 2026 lesen und schreiben, ungeprüft | hoch, aber frühe Version |
| Pluton | GPL-3.0 | v0.11, Alpha | nein | hoch, Alpha |
| FreeCAD | LGPL-2.1 | 1.1.1, April 2026 | nur mit Add-on freecad-openskp | niedrig, parametrisch |
| Blender | GPL | 5.2 LTS | nur mit Add-on | niedrig bis mittel, mit Add-ons wie CAD Sketcher oder Construction Lines besser |
| SolveSpace, Wings3D, Sweet Home 3D, OpenSCAD, BRL-CAD, Dust3D | frei | unterschiedlich | nein | niedrig |

SketchUp Make 2017, die letzte kostenlose Desktop-Version, bietet Trimble nicht mehr an. Sie öffnet nur Dateien bis zum 2017-Format.

## 2. Das Dateiformat

- **Bis 2020** war `.skp` ein binärer Objektstrom (MFC CArchive), jede Version mit eigenem Format.
- **Ab 2021** gibt es ein einheitliches neues Format: ein Kopf mit Versionsnummer, danach ein ZIP-Container mit `model.dat`, Materialordnern und Vorschaubild. SketchUp 2021 kann daher auch Dateien aus 2026 öffnen, ältere Versionen aber nicht.
- Trimble hat das Format nie offengelegt. Alle offenen Leser beruhen auf Reverse Engineering.
- Die Versionsnummer steht im Dateikopf, zum Beispiel `{26.1.194}`. `skptool info` liest sie direkt aus.

## 3. Wege von `.skp` zu offenen Formaten

| Weg | Kosten | Erhalten | Automatisierbar |
|---|---|---|---|
| SketchUp Free (Web), Download als STL | gratis | nur Geometrie | nein |
| SketchUp Go | 129 $ pro Jahr | OBJ, DAE, FBX, DWG | nein |
| SketchUp Pro, Testversion | gratis für die Dauer der Testphase | alle Formate | über Ruby-Erweiterungen in SketchUp |
| **OpenSKP** (Python, JS, .NET, Dart, C++) | gratis, MIT | Geometrie, Materialien, Texturen, Komponenten, Ebenen | ja |
| Blender mit RedHalo Sketchup_Importer 0.27 | gratis, GPL | Geometrie, Materialien, Texturen, Komponenten | ja, nur Windows und macOS |
| Blender mit blender-openskp 0.2.5 | gratis | Import und Export | ja, sehr neu |
| Innerscene SKP Converter (im Browser, ohne Upload) | gratis | Geometrie und Farben | nein |
| ImageToSTL und ähnliche Online-Konverter | gratis | unterschiedlich | nein, Datei wird hochgeladen |
| Okino PolyTrans | kommerziell | viel | ja |
| Unity Personal | gratis bis Umsatzgrenze | Materialien, Komponenten, keine Ebenen | ja |

Twinmotion und D5 Render importieren `.skp`, exportieren aber keine Geometrie. CAD Exchanger und Datakit unterstützen `.skp` nicht.

## 4. Blender-Add-ons im Detail

- **RedHaloStudio/Sketchup_Importer 0.27.0** (Januar 2026) ist das verbreitete Import-Add-on. Es nutzt das offizielle Trimble-SDK, läuft laut Projekt unter Windows und macOS und unterstützt Blender 5.1 und 5.2 sowie Dateien bis SketchUp 2026. Nach den ausgewerteten Quellen importiert es, exportiert aber nicht. Hier nicht selbst getestet.
- **blender-openskp 0.2.5** vom Autor von OpenSKP importiert und exportiert ohne SDK, auch unter Linux. Es bündelt OpenSKP 1.3.0, das zum Zeitpunkt der Recherche erst wenige Tage alt war. Mit der älteren OpenSKP-Version 1.2.0 bricht der Import ab. `skptool` verwendet nur Paketversionen, die mindestens 14 Tage veröffentlicht sind, und nutzte deshalb zunächst 1.2.0. OpenSKP 1.3.0 hat es erst nach Prüfung von Paket und Quellcode als ausdrückliche Ausnahme übernommen. Das Add-on selbst nutzt `skptool` nicht.
- **Skp Editor** auf Superhive kostet 16 $ und kann laut Anbieter Import und Export mit Texturen. Nicht getestet.
- blender-openskp exportiert laut eigener Doku (Stand September 2026) die Geometrie flach und in Dreiecken; Texturen gehen nur als Volltonfarbe zurück, Tags aus Collections ja. Harte und weiche Kanten, Rückseiten und Tönung werden nicht genannt.
- Auf extensions.blender.org gibt es noch kein SketchUp-Add-on. blender-openskp wartet dort auf Freigabe.

## 4a. KI-Werkzeuge (Nachtrag September 2026)

- **Blender MCP** (ahujasid/blender-mcp) und der Blender-Connector für Claude steuern ein offenes Blender über dessen Python-Schnittstelle. `.skp` lesen oder schreiben sie nicht selbst.
- **SketchUp-Connector für Claude** (Trimble, seit April 2026): Claude erzeugt aus Text und Bildern neue SketchUp-Modelle und gibt eine `.skp` zum Herunterladen. Ohne SketchUp-Abo 30 Dateien im Monat. Ob bestehende Dateien bearbeitet werden können, ist nicht dokumentiert.
- Community-Server wie sketchup-mcp brauchen ein laufendes SketchUp (Desktop, Ruby).
- `skptool` bearbeitet dagegen vorhandene `.skp` ohne SketchUp: über Blender im Live-Modus oder per MCP-Server, und schreibt das Ergebnis mit Struktur zurück.

## 5. Das offizielle Trimble-SDK

- Die C-Schnittstelle kann `.skp` vollständig lesen und schreiben, ohne dass SketchUp installiert ist. Aktuell ist Version 14.2 für SketchUp 2026.2.
- Es gibt sie nur für Windows und macOS, nicht für Linux.
- Der Download erfordert ein Entwicklerkonto. Nach Berichten im SketchUp-Forum war er 2026 zeitweise nicht verfügbar.
- Die Trimble Developer Terms regeln Weitergabe und Einsatz des SDK. Nach unserem Verständnis passen sie nicht zu einem frei weitergegebenen Open-Source-Werkzeug. Maßgeblich sind die Bedingungen selbst (Link unten), das hier ist keine Rechtsberatung.

## 6. Entscheidung für die CLI

Gebaut wurde `skptool` auf **OpenSKP plus Blender** (anfangs OpenSKP 1.2.0, inzwischen 1.3.0):

- OpenSKP liest alle getesteten Versionen und schreibt `.skp`. Es ist MIT-lizenziert und läuft ohne SketchUp und ohne SDK.
- Blender liefert die Bearbeitung und Formate wie FBX, USD und `.blend`. Es läuft dafür im Hintergrund ohne Fenster.
- Das Trimble-SDK wurde verworfen, weil es schwer erhältlich ist und seine Bedingungen nicht zu einem offenen Werkzeug passen.

Beim Bau mit OpenSKP 1.2.0 wurden fünf Probleme gefunden und in `skptool` umgangen:

1. Die eingebaute automatische Triangulierung schreibt Dateien, die der eigene Leser nicht mehr öffnet. `skptool` trianguliert deshalb selbst.
2. Die Bearbeitungsfunktion lehnt Dateien ab 2021 ab. `skptool` nutzt dieselbe Daten-Wiedergabe ohne diese Sperre.
3. Beim Umschreiben ging die Deckkraft verloren, Glas wurde undurchsichtig.
4. Bei Dateien vor 2021 fehlen im Szenenaufbau die Ebenen und die Bemalung von Gruppen, obwohl der Leser sie kennt.
5. Unbemalte Flächen bekommen die Ebenenfarbe statt der SketchUp-Standardfarbe, und Vorder- und Rückseite kommen als doppelte Flächen.

Mit OpenSKP 1.3.0 bleiben die eigene Triangulierung (die neue mit earcut legt bei einem Testmodell 447 Dreiecke ohne Fläche an) und die Ergänzung von Ebenen und Gruppenbemalung im Szenenaufbau nötig. Der eigene Ersatz der Texturbasis entfällt, weil der Writer seit 1.3.0 dieselbe Texturbasis wie der Leser nutzt.

Diese Punkte sollen als Hinweise an das OpenSKP-Projekt gehen.

## 7. Performance-Optimierung

Mit echten Projektdateien (22 MB und 201 MB aus SketchUp 2026.2) zeigten sich diese Engpässe:

1. **OpenSKP liest doppelt.** `parse()` liest bei jedem Aufruf die ganze Datei neu, `build_scene()` ebenfalls. `skptool` liest jetzt nur noch einmal ein.
2. **Ausgerechnete Szene statt Instanzen.** Jede Platzierung einer Komponente wurde als eigene Geometrie geschrieben. Die instanzerhaltende Szene von OpenSKP enthält beim 22-MB-Modell ein Drittel der Dreiecke, und Komponenten bleiben in Blender verknüpft.
3. **trimesh als GLB-Writer** wandelt jede Ecke einzeln in Python um. Ein eigener numpy-Writer braucht 0,1 statt 23 Sekunden.
4. **Blender-Operatoren pro Objekt** wachsen quadratisch mit der Objektzahl. Ersetzt durch numpy und Datenzugriffe, die nur noch einmal pro eindeutigem Mesh laufen.
5. **Riesiges JSON für den Rückweg.** Ersetzt durch ein binäres Format, das eine Definition nach der anderen liest. Der Rückweg für die 201-MB-Datei braucht jetzt unter 1 GB statt über 27 GB.
6. **Gescherte Matrizen.** Blender kann Scherung nicht in einem Objekt speichern. Sie wird exakt in zwei verschachtelte Knoten zerlegt.

Die Node.js-Variante von OpenSKP (1.2.0, alle Abhängigkeiten älter als 14 Tage) liest das 22-MB-Modell in 4,9 statt 28 Sekunden. Bei der 201-MB-Datei bricht sie aber nach über 20 GB ab, und ihr Writer stürzt ab etwa 370.000 Flächen ab. Sie wurde deshalb nicht eingebaut. Der verbleibende größte Posten ist das Einlesen durch OpenSKP in Python.

## Quellen

- OpenSKP: https://github.com/iamahsanmehmood/openskp
- blender-openskp: https://github.com/iamahsanmehmood/blender-openskp
- freecad-openskp: https://github.com/iamahsanmehmood/freecad-openskp
- Hew: https://hew3d.com und https://github.com/hew3d/hew
- Hew/openskp (Rust): https://github.com/hew3d/openskp
- IngeTrazo: https://github.com/ingelibre/ingetrazo
- Pluton: https://github.com/Parrow-Horrizon-Studio/pluton
- RedHalo Sketchup_Importer: https://github.com/RedHaloStudio/Sketchup_Importer/releases
- Blender MCP: https://github.com/ahujasid/blender-mcp
- SketchUp-Connector für Claude: https://www.engineering.com/now-sketchup-has-an-mcp-server-for-claude-based-3d-modeling/
- SketchUp Free Funktionsumfang: https://sketchup.trimble.com/en/plans-and-pricing/sketchup-free
- SketchUp Make nicht mehr verfügbar: https://help.sketchup.com/en/make-access
- SketchUp C API Release Notes: https://extensions.sketchup.com/developers/sketchup_c_api/sketchup/md__sketch_up__c__a_p_i__release__notes.html
- Trimble Developer Terms: https://www.trimble.com/en/legal/developer-terms
- SDK-Verfügbarkeit im Forum: https://forums.sketchup.com/t/sketchup-c-sdk-2026-macos-unavailable/346672
- Innerscene SKP Converter: https://www.innerscene.com/tools/skp-converter
- FreeCAD zum SketchUp-Import: https://github.com/FreeCAD/FreeCAD-documentation/blob/main/wiki/Importing_From_Sketchup.md
