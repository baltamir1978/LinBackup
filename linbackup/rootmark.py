"""La marca de la carpeta raíz: de qué equipo y de qué app es.

El mismo SSD se usa con BackupSSD en el Mac y con LinBackup en Linux (Bruno, 21-oct-2026).
Si los dos escribieran en la misma raíz, cada uno apartaría a `_Retirados` lo que puso el
otro, porque no lo tiene en su lado. Así que cada equipo tiene la suya, y LinBackup **se
niega** a usar una raíz que no lleve su marca y no esté vacía: casi seguro que es la de
otro equipo. BackupSSD deja la suya (`.backupssd-raiz.json`) desde octubre de 2026; una
`Backup-SSD` de antes no la lleva, y cae en el caso de «con cosas y sin marca».
"""

import json
import os
import socket
from datetime import datetime
from pathlib import Path
from typing import Any

from .report import ForeignRoot

MARK_FILE = ".linbackup-raiz.json"
APP = "LinBackup"
# La que deja BackupSSD en la suya desde octubre de 2026 (mismas claves).
BACKUPSSD_MARK_FILE = ".backupssd-raiz.json"


def machine_id() -> str:
    """Identifica este equipo aunque cambie de nombre (si hay machine-id, que en Debian
    siempre lo hay)."""
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
            if value:
                return value
        except OSError:
            continue
    return f"host:{socket.gethostname()}"


def read_mark(root: Path, file: str = MARK_FILE) -> dict[str, Any] | None:
    try:
        data = json.loads((root / file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_mark(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    mark = {"app": APP, "equipo": socket.gethostname(), "machine_id": machine_id(),
            "creada": datetime.now().isoformat(timespec="seconds"),
            "aviso": "Esta carpeta la mantiene LinBackup desde el equipo de arriba. Otro "
                     "equipo no debe copiar aquí: retiraría lo que no tiene."}
    (root / MARK_FILE).write_text(json.dumps(mark, ensure_ascii=False, indent=2) + "\n",
                                  encoding="utf-8")


def _is_empty(root: Path) -> bool:
    try:
        return not any(name != MARK_FILE for name in os.listdir(root))
    except OSError:
        return True


def check(root: Path, dry_run: bool, adopt: bool = False) -> None:
    """Deja usar la raíz o lanza `ForeignRoot` con el motivo.

    - No existe, o está vacía: es nueva; se marca (salvo en un ensayo, que no escribe).
    - Lleva la marca de este equipo: adelante.
    - Lleva la marca de otro equipo, o no lleva marca y tiene cosas: no se toca, salvo que
      se pida expresamente adoptarla (`adopt`), que la marca como de este equipo.
    """
    if not root.exists() or _is_empty(root):
        if not dry_run:
            write_mark(root)
        return
    mac = read_mark(root, BACKUPSSD_MARK_FILE)
    if mac is not None:
        # La del Mac no se adopta nunca, ni con --adoptar: es de otro equipo seguro.
        raise ForeignRoot(
            f"La carpeta «{root.name}» del disco es la de BackupSSD, del Mac "
            f"{mac.get('equipo', '?')}. Cada equipo necesita la suya: elige otra carpeta raíz.")
    mark = read_mark(root)
    if mark is not None and mark.get("app") == APP and mark.get("machine_id") == machine_id():
        return
    if adopt:
        if not dry_run:
            write_mark(root)
        return
    if mark is None:
        raise ForeignRoot(
            f"La carpeta «{root.name}» del disco ya tiene cosas y no es de LinBackup (puede ser "
            "la de BackupSSD del Mac). Si se copiara aquí, se retirarían los archivos que no "
            "están en este equipo. Elige otra carpeta raíz, o adóptala con --adoptar si de "
            "verdad es la de este equipo.")
    raise ForeignRoot(
        f"La carpeta «{root.name}» del disco es de otro equipo ({mark.get('equipo', '?')}). "
        "Cada equipo necesita la suya: elige otra carpeta raíz.")
