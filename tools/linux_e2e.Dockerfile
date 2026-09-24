# syntax=docker/dockerfile:1
# Linux-Pruefumgebung fuer tools/linux_e2e.sh: Python 3.12 und Blender 5.2.0, ohne Bildschirm.
# Das Projekt selbst kommt nicht ins Image, es wird beim Lauf nur lesend eingehaengt.
#
# Lieferkette:
#  - Basisimage per Digest gepinnt: python:3.12.13-slim (Debian trixie), zuletzt gebaut am 2026-08-07,
#    also mindestens 14 Tage alt. Das rollende Tag 3.12-slim wird absichtlich nicht benutzt.
#  - Blender per SHA-256 geprueft, dieselbe Datei und Pruefsumme wie in .github/workflows/tests.yml.
#  - Python-Pakete werden erst im Lauf installiert, nur aus requirements.lock mit --require-hashes.
FROM python:3.12.13-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

# Systembibliotheken fuer Blender im Hintergrund (wie im CI-Job), xz zum Entpacken
RUN apt-get update -q \
 && apt-get install -y -q --no-install-recommends \
      libegl1 libgl1 libgl1-mesa-dri libegl-mesa0 libxi6 libxkbcommon0 libxxf86vm1 libxfixes3 \
      libxrender1 libsm6 xz-utils \
 && rm -rf /var/lib/apt/lists/*

ADD --checksum=sha256:96f6c181a30f4950607839dc84d42a354b250d8a0231b098b59b7bc69c351c48 \
    https://download.blender.org/release/Blender5.2/blender-5.2.0-linux-x64.tar.xz /tmp/blender.tar.xz
RUN mkdir -p /opt/blender \
 && tar -xf /tmp/blender.tar.xz -C /opt/blender --strip-components=1 \
 && rm /tmp/blender.tar.xz \
 && /opt/blender/blender --version | head -1

# Ohne root pruefen: Dateirechte (private Statusdateien usw.) verhalten sich sonst anders
RUN useradd --create-home --uid 1000 pruefer
USER pruefer
ENV SKPTOOL_BLENDER=/opt/blender/blender \
    SKPTOOL_SKIP_GUI_TESTS=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /home/pruefer
