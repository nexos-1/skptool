#!/bin/sh
# Linux-Pruefung mit echten Dateien in Docker (Python 3.12, Blender 5.2.0, virtueller Bildschirm Xvfb).
#
# Aufruf aus dem Projektordner (Linux, macOS oder Git Bash unter Windows, Docker muss laufen):
#   sh tools/linux_e2e.sh [AUSGABEORDNER]             alles (Schritte 1 bis 6)
#   sh tools/linux_e2e.sh --nur-live [AUSGABEORDNER]  nur Schritte 1 und 3 (Live-Modus mit Fenster)
# Vorher fuer alle Tests die externen Beispiele laden: python tools/beispiele_laden.py
#
# Das Projekt wird nur lesend eingehaengt (/src), Ergebnisse landen im Ausgabeordner (/out, Standard: ein
# neuer temporaerer Ordner). Im Container, ohne root:
#   1. Abhaengigkeiten nur aus requirements.lock mit --require-hashes in eine eigene venv
#   2. ganze Testsuite mit Blender (ohne Fenster-Tests)
#   3. Fenster-Tests des Live-Modus (tests.test_live, dazu die live_*-Werkzeuge in tests.test_mcp) mit
#      echtem Blender-Fenster auf einem virtuellen Bildschirm (xvfb-run, OpenGL per Software aus Mesa)
#   4. Paket bauen (setuptools mit Pruefsumme aus tools/paketbau.lock) und in eine frische venv installieren
#   5. mit dem installierten Befehl skptool aus einem Ordner voller praeparierter Python-Dateien:
#      convert --jobs auto nach .glb und .blend, Rueckweg nach .skp, diff, report, mcp
#   6. Speicherwaechter mit Speichergrenze des Containers (docker --memory)
# Rueckgabewert 0 nur, wenn alles gruen ist.
set -eu

if [ "${1:-}" != "--im-container" ]; then
    # ---------------------------------------------------------------- auf dem Rechner
    export MSYS_NO_PATHCONV=1  # Git Bash: Pfade wie /src nicht in Windows-Pfade umschreiben
    MODUS=alles
    if [ "${1:-}" = "--nur-live" ]; then
        MODUS=live
        shift
    fi
    cd "$(dirname "$0")/.."
    REPO=$(pwd -W 2>/dev/null || pwd)
    OUT=${1:-$(mktemp -d)}
    mkdir -p "$OUT"
    OUT=$(cd "$OUT" && (pwd -W 2>/dev/null || pwd))
    BILD=skptool-linux-e2e
    echo "Baue $BILD (Blender-Download nur beim ersten Mal) ..."
    docker build -q -t "$BILD" - < tools/linux_e2e.Dockerfile
    echo "Ausgabe: $OUT"
    # --init holt beendete Kindprozesse ab (sonst bleiben im Container Zombies stehen);
    # --memory setzt eine Speichergrenze, die der Speicherwaechter erkennen muss;
    # eigener Name je Lauf, damit nie ein fremder Container gemeint ist
    exec docker run --rm --init --memory=6g --name "skptool-linux-e2e-$$" \
        --mount "type=bind,src=$REPO,dst=/src,readonly" \
        --mount "type=bind,src=$OUT,dst=/out" \
        "$BILD" sh /src/tools/linux_e2e.sh --im-container "$MODUS"
fi

# ---------------------------------------------------------------- im Container
MODUS=${2:-alles}
SRC=/src
OUT=/out
ARBEIT=$(mktemp -d)
FEHLER=0
schritt() { printf '\n=== %s\n' "$*"; }
fehler() { echo "FEHLER: $*"; FEHLER=$((FEHLER + 1)); }

schritt "Umgebung"
python --version
"$SKPTOOL_BLENDER" --version | head -1
echo "Kerne: $(nproc)"

schritt "Abhaengigkeiten (nur mit Pruefsummen)"
python -m venv "$ARBEIT/venv"
PY="$ARBEIT/venv/bin/python"
"$PY" -m pip install -q --require-hashes --no-deps -r "$SRC/requirements.lock"
"$PY" -m pip list --format=freeze

if [ "$MODUS" = alles ]; then
    schritt "Testsuite mit Blender (Projektordner nur lesend)"
    if (cd "$SRC" && "$PY" -m unittest discover -s tests) > "$OUT/tests.log" 2>&1; then
        tail -4 "$OUT/tests.log"
    else
        tail -40 "$OUT/tests.log"
        fehler "Testsuite"
    fi
fi

schritt "Live-Modus mit Blender-Fenster auf virtuellem Bildschirm (Xvfb)"
# xvfb-run -a sucht eine freie Anzeige und startet Xvfb (Standard: 1280x1024, 24 Bit), beendet ihn danach.
# env -u schaltet die Fenster-Tests ein, die das Image sonst abschaltet.
if (cd "$SRC" && timeout 1800 xvfb-run -a env -u SKPTOOL_SKIP_GUI_TESTS \
        "$PY" -m unittest tests.test_live tests.test_mcp -v) > "$OUT/live.log" 2>&1; then
    grep -E "^  (Live-Blender|ops-Latenz|Bild model|Speichern|quit)" "$OUT/live.log" || true
    tail -4 "$OUT/live.log"
else
    tail -60 "$OUT/live.log"
    fehler "Live-Modus mit Fenster"
fi
# Gruen allein reicht nicht: uebersprungene Fenster-Tests waeren auch gruen
if ! grep -q "Live-Blender bereit nach" "$OUT/live.log" || grep -q "GUI-Tests abgeschaltet" "$OUT/live.log"; then
    fehler "Fenster-Tests sind nicht gelaufen (uebersprungen?)"
fi

if [ "$MODUS" = live ]; then
    schritt "Ergebnis"
    if [ "$FEHLER" -ne 0 ]; then
        echo "ROT: $FEHLER Fehler"
        exit 1
    fi
    echo "GRUEN"
    exit 0
fi

schritt "Paket bauen und installieren"
mkdir -p "$ARBEIT/quelle"
cp -r "$SRC/pyproject.toml" "$SRC/MANIFEST.in" "$SRC/README.md" "$SRC/LICENSE" "$SRC/skptool" "$ARBEIT/quelle/"
python -m venv "$ARBEIT/bau"
"$ARBEIT/bau/bin/python" -m pip install -q --require-hashes --no-deps -r "$SRC/tools/paketbau.lock"
"$ARBEIT/bau/bin/python" -m pip wheel -q --no-build-isolation --no-deps -w "$ARBEIT/dist" "$ARBEIT/quelle"
ls "$ARBEIT/dist"
python -m venv "$ARBEIT/paket"
"$ARBEIT/paket/bin/python" -m pip install -q --require-hashes --no-deps -r "$SRC/requirements.lock"
"$ARBEIT/paket/bin/python" -m pip install -q --no-deps "$ARBEIT"/dist/skptool-*.whl
SK="$ARBEIT/paket/bin/skptool"
head -1 "$SK"

schritt "Installierter Befehl: Tests (Wheel, Suchpfad aus feindlichem Ordner)"
if (cd "$SRC" && SKPTOOL_INSTALLIERT="$SK" "$PY" -m unittest tests.test_paket -v) > "$OUT/paket.log" 2>&1; then
    tail -4 "$OUT/paket.log"
else
    tail -40 "$OUT/paket.log"
    fehler "test_paket"
fi

# Ab hier laeuft alles aus einem Ordner mit praeparierten Python-Dateien
FEIND="$ARBEIT/feindlich"
mkdir -p "$FEIND/skptool"
for m in numpy json re glob argparse subprocess openskp; do
    echo "open('$ARBEIT/PWNED_$m', 'w')" > "$FEIND/$m.py"
done
echo "open('$ARBEIT/PWNED_skptool', 'w')" > "$FEIND/skptool/__init__.py"
cd "$FEIND"

EINGABEN=$(ls "$SRC"/samples/*.skp "$SRC"/samples/extern/*.skp 2>/dev/null || true)
ANZAHL=$(echo "$EINGABEN" | grep -c . || true)
schritt "convert $ANZAHL Dateien nach .glb, --jobs auto"
echo "$EINGABEN"
# shellcheck disable=SC2086
if ! "$SK" convert $EINGABEN -f glb -d "$OUT/glb" --jobs auto --force; then fehler "convert glb"; fi
[ "$(ls "$OUT"/glb/*.glb 2>/dev/null | wc -l)" -eq "$ANZAHL" ] || fehler "nicht alle .glb geschrieben"

schritt "convert $ANZAHL Dateien nach .blend, --jobs auto (Blender)"
# shellcheck disable=SC2086
if ! "$SK" convert $EINGABEN -f blend -d "$OUT/blend" --jobs auto --force; then fehler "convert blend"; fi
[ "$(ls "$OUT"/blend/*.blend 2>/dev/null | wc -l)" -eq "$ANZAHL" ] || fehler "nicht alle .blend geschrieben"

schritt "Zum Vergleich: dieselben .glb nacheinander"
START=$(date +%s)
# shellcheck disable=SC2086
"$SK" convert $EINGABEN -f glb -d "$ARBEIT/glb_seriell" -q || fehler "convert glb nacheinander"
echo "nacheinander: $(( $(date +%s) - START )) s"

schritt "Rueckweg .blend -> .skp und diff"
"$SK" convert "$OUT/blend/stuhl_tisch_2017.blend" -o "$OUT/stuhl_zurueck.skp" || fehler "blend -> skp"
"$SK" diff "$SRC/samples/stuhl_tisch_2017.skp" "$SRC/samples/stuhl_tisch_2017.skp" -q || fehler "diff gleich"
set +e
"$SK" diff "$SRC/samples/stuhl_tisch_2017.skp" "$OUT/stuhl_zurueck.skp" --geometrie
RC=$?
set -e
echo "diff Rueckgabewert: $RC (0 gleich, 1 verschieden, 2 Fehler)"
[ "$RC" -le 1 ] || fehler "diff Rueckweg"

schritt "report"
# shellcheck disable=SC2086
"$SK" report $EINGABEN "$OUT/stuhl_zurueck.skp" -o "$OUT/bericht.html" || fehler "report html"
# shellcheck disable=SC2086
"$SK" report $EINGABEN --json -o "$OUT/bericht.json" || fehler "report json"
# shellcheck disable=SC2086
"$SK" report $EINGABEN || fehler "report text"

schritt "mcp (Testclient gegen den installierten Befehl)"
"$PY" "$SRC/tools/mcp_testclient.py" -- "$SK" mcp --nur-lesen || fehler "mcp"

schritt "Speicherwaechter mit Speichergrenze des Containers"
"$ARBEIT/paket/bin/python" -P -c "
from skptool import stapel
m = stapel._frei_linux()
c = stapel._frei_cgroup()
f = stapel.freier_speicher()
gb = lambda x: 'keine Grenze' if x is None else f'{x / 2**30:.1f} GB'
print('meminfo:', gb(m), '| cgroup:', gb(c), '| genutzt:', gb(f))
assert c is not None, 'Container ohne erkannte Speichergrenze (docker run --memory)'
assert f == min(m, c)
"  || fehler "Speicherwaechter"

schritt "Keine Module aus dem aktuellen Ordner geladen"
if ls "$ARBEIT"/PWNED_* >/dev/null 2>&1; then
    ls "$ARBEIT"/PWNED_*
    fehler "Module aus dem aktuellen Ordner geladen"
else
    echo "keine"
fi

schritt "Ergebnis"
ls -la "$OUT" "$OUT/glb" "$OUT/blend"
if [ "$FEHLER" -ne 0 ]; then
    echo "ROT: $FEHLER Fehler"
    exit 1
fi
echo "GRUEN"
