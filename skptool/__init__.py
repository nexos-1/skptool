"""skptool - SketchUp (.skp) Dateien ohne SketchUp lesen, konvertieren und zurueckschreiben."""
import os as _os
import sys as _sys

__version__ = "0.1.0"


def _suchpfad_haerten():
    """Zweite Verteidigungslinie neben skptool.cmd und -P: Eintraege in sys.path, die fuer den aktuellen
    Ordner stehen ("", "." oder derselbe Ordner absolut), fliegen raus. So kann eine praeparierte glob.py
    neben einem Modell nicht nachgeladen werden. Ausnahme: der Ordner, in dem das Paket skptool liegt
    (Start aus dem Projektordner, etwa bei den Tests)."""
    def echt(p):
        try:
            return _os.path.normcase(_os.path.realpath(p or "."))
        except (OSError, ValueError):
            return None
    try:
        hier = echt(_os.getcwd())
    except OSError:  # aktueller Ordner geloescht: dann steht auch nichts dafuer im Suchpfad
        return
    paket = echt(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    if hier == paket:
        return
    _sys.path[:] = [p for p in _sys.path if not (isinstance(p, str) and (p in ("", ".") or echt(p) == hier))]


_suchpfad_haerten()
