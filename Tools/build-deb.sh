#!/usr/bin/env bash
# Monta el paquete .deb de LinBackup en build/.
#
#   Tools/build-deb.sh            → build/linbackup_<versión>+<AAMMDDHHMM>_all.deb
#   Tools/build-deb.sh --check    → además, comprueba el paquete por dentro
#
# Funciona igual en Debian que en el Mac (dpkg-deb de Homebrew: brew install dpkg).
# El paquete es `Architecture: all`: sólo lleva Python, no hay nada que compilar.
# Se instala en Debian con: sudo apt install ./build/linbackup_…_all.deb
set -euo pipefail

cd "$(dirname "$0")/.."

command -v dpkg-deb >/dev/null || { echo "✗ Falta dpkg-deb (Debian: viene de serie; Mac: brew install dpkg)"; exit 1; }

VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' linbackup/__init__.py)
[ -n "$VERSION" ] || { echo "✗ No encuentro __version__ en linbackup/__init__.py"; exit 1; }
# Fecha y hora como número de compilación, igual que en TestFlight: siempre crece.
BUILD=$(date +%y%m%d%H%M)
FULL="$VERSION+$BUILD"
NAME="linbackup_${FULL}_all"
ROOT="build/$NAME"

rm -rf "$ROOT" "build/$NAME.deb"
mkdir -p "$ROOT/DEBIAN" "$ROOT/usr/bin" "$ROOT/usr/lib/linbackup" "$ROOT/usr/share/doc/linbackup"

# El código, sin cachés ni basura del Mac.
mkdir -p "$ROOT/usr/lib/linbackup/linbackup"
for f in linbackup/*.py; do
    install -m 644 "$f" "$ROOT/usr/lib/linbackup/linbackup/"
done

# El lanzador usa el Python del sistema (el de Debian 13 es el 3.13).
cat > "$ROOT/usr/bin/linbackup" <<'EOF'
#!/usr/bin/python3
import sys
sys.path.insert(0, "/usr/lib/linbackup")
from linbackup.__main__ import main
sys.exit(main())
EOF
chmod 755 "$ROOT/usr/bin/linbackup"

cat > "$ROOT/usr/share/doc/linbackup/copyright" <<'EOF'
LinBackup
Copyright 2026 Bruno Altamirano
Código: https://github.com/baltamir1978/LinBackup
EOF
chmod 644 "$ROOT/usr/share/doc/linbackup/copyright"

# Los .pyc se generan al instalar y se borran al desinstalar: así dpkg no tiene que
# conocerlos y no quedan restos.
cat > "$ROOT/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    python3 -m compileall -q /usr/lib/linbackup >/dev/null 2>&1 || true
fi
EOF
cat > "$ROOT/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
find /usr/lib/linbackup -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
EOF
chmod 755 "$ROOT/DEBIAN/postinst" "$ROOT/DEBIAN/prerm"

SIZE=$(du -sk "$ROOT/usr" | cut -f1)
cat > "$ROOT/DEBIAN/control" <<EOF
Package: linbackup
Version: $FULL
Section: utils
Priority: optional
Architecture: all
Depends: python3 (>= 3.11), util-linux
Installed-Size: $SIZE
Maintainer: Bruno Altamirano <baltamir1978@users.noreply.github.com>
Homepage: https://github.com/baltamir1978/LinBackup
Description: copia tus carpetas a un SSD externo en cuanto lo enchufas
 Reconoce el disco por su UUID y nunca por su nombre, aparta lo que borras en
 vez de eliminarlo (_Retirados/AAAA-MM-DD, con purga a los N días), planifica
 antes de escribir y compara byte a byte antes de mover nada. Por ahora sólo en
 modo consola: linbackup --help
EOF

# Directorios a 755 y propietario root en el paquete, se construya donde se construya.
find "$ROOT" -type d -exec chmod 755 {} +
COPYFILE_DISABLE=1 dpkg-deb --root-owner-group -Zxz --build "$ROOT" "build/$NAME.deb" >/dev/null
rm -rf "$ROOT"
echo "✓ build/$NAME.deb"

if [ "${1:-}" = "--check" ]; then
    dpkg-deb --info "build/$NAME.deb" | sed -n '/Package:/,$p'
    echo "--- contenido"
    dpkg-deb --contents "build/$NAME.deb"
    # Que no se cuele nada del Mac ni ningún .pyc.
    if dpkg-deb --contents "build/$NAME.deb" | grep -E '\.DS_Store|/\._|__pycache__|\.pyc$'; then
        echo "✗ El paquete lleva ficheros que no debería"; exit 1
    fi
    # Que lo empaquetado arranque: se extrae y se ejecuta con un Python moderno.
    TMP=$(mktemp -d)
    dpkg-deb -x "build/$NAME.deb" "$TMP"
    PY=$(command -v python3.13 || command -v python3.14 || command -v python3)
    PYTHONPATH="$TMP/usr/lib/linbackup" "$PY" -m linbackup --version
    PYTHONPATH="$TMP/usr/lib/linbackup" "$PY" -m linbackup --help | head -1
    rm -rf "$TMP"
    echo "✓ El paquete arranca"
fi
