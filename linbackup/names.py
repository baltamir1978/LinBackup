"""Nombres de archivo: cuándo dos son el mismo para el disco, y cuáles no admite.

En el Mac no hacía falta casi nada de esto: APFS no distingue mayúsculas, igual que el
exFAT del disco. En Linux el origen suele ser ext4, que sí las distingue, y el destino un
exFAT que no; y los nombres pueden traer cosas que exFAT rechaza.
"""

import os
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

# Sistemas de archivos de Windows: sin mayúsculas, con caracteres prohibidos.
_FAT_LIKE = {"exfat", "vfat", "msdos", "fat", "fat32", "ntfs", "ntfs3", "fuseblk"}
_CASE_SENSITIVE = {"ext2", "ext3", "ext4", "btrfs", "xfs", "zfs", "f2fs", "tmpfs", "jfs",
                   "reiserfs", "nilfs2", "bcachefs"}

# Lo que exFAT, FAT y NTFS (desde Windows) no admiten en un nombre, además de los
# caracteres de control.
_FORBIDDEN = set('"*/:<>?\\|')


@dataclass(frozen=True)
class FilesystemInfo:
    fstype: str            # «exfat», «ext4», «apfs»…; «» si no se sabe
    mount_point: str

    @property
    def restricted_names(self) -> bool:
        """¿Rechaza los nombres que Windows no admite?"""
        return self.fstype in _FAT_LIKE


def _existing_ancestor(path: Path) -> Path:
    path = path.absolute()
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def _mounts() -> list[tuple[str, str]]:
    """(punto de montaje, tipo) de todo lo montado."""
    if sys.platform.startswith("linux"):
        out = []
        try:
            with open("/proc/self/mounts", encoding="utf-8") as handle:
                for line in handle:
                    parts = line.split()
                    if len(parts) >= 3:
                        # Los espacios van como «\040».
                        out.append((parts[1].replace("\\040", " "), parts[2]))
        except OSError:
            pass
        return out
    try:
        text = subprocess.run(["mount"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for line in text.splitlines():
        # macOS: «/dev/disk4s1 on /Volumes/X Y (exfat, local, nodev…)»
        if " on " not in line or " (" not in line:
            continue
        rest = line.split(" on ", 1)[1]
        mount_point, _, options = rest.rpartition(" (")
        out.append((mount_point, options.split(",")[0].strip()))
    return out


def filesystem_of(path: Path) -> FilesystemInfo:
    target = str(_existing_ancestor(path).resolve())
    best = ("", "")
    for mount_point, fstype in _mounts():
        prefix = mount_point.rstrip("/") + "/"
        if (target == mount_point or target.startswith(prefix) or mount_point == "/") \
                and len(mount_point) >= len(best[0]):
            best = (mount_point, fstype)
    return FilesystemInfo(fstype=best[1].lower(), mount_point=best[0])


def is_case_insensitive(directory: Path, may_write: bool) -> bool:
    """¿El disco toma por el mismo archivo `Foto.jpg` y `foto.jpg`?

    Primero se mira con algo que ya esté (sin escribir nada); luego por el tipo de sistema
    de archivos; y, si se puede escribir, con un archivo de prueba. Si no hay forma de
    saberlo, se supone que **no** las distingue: tratar como uno lo que serían dos sólo
    cuesta una copia; lo contrario, perder un archivo.
    """
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                swapped = entry.name.swapcase()
                if swapped != entry.name:
                    try:
                        return os.path.samestat(os.lstat(entry.path),
                                                os.lstat(os.path.join(directory, swapped)))
                    except OSError:
                        return False
    except OSError:
        pass
    fstype = filesystem_of(directory).fstype
    if fstype in _FAT_LIKE:
        return True
    if fstype in _CASE_SENSITIVE:
        return False
    if may_write and directory.is_dir():
        probe = directory / ".linbackup-sondeo-Aa"
        try:
            probe.touch()
            try:
                return (directory / ".LINBACKUP-SONDEO-AA").exists()
            finally:
                probe.unlink(missing_ok=True)
        except OSError:
            pass
    return True


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _simple_upper(text: str) -> str:
    # Carácter a carácter y sólo si sigue siendo uno: «ß».upper() da «SS», y exFAT no
    # considera iguales «ß» y «SS» (su tabla de mayúsculas va letra a letra).
    return "".join(u if len(u := ch.upper()) == 1 else ch for ch in text)


def pairing_key(rel: str, case_insensitive: bool) -> str:
    """Para emparejar un nombre del ordenador con el que ya hay en el disco.

    Siempre en NFC: el Mac suele escribir «canción» descompuesta (NFD) y Linux compuesta
    (NFC). Para exFAT en Linux son dos nombres distintos, así que sin esto un archivo
    llegado del Mac se retiraría y se volvería a copiar.
    """
    key = nfc(rel)
    return _simple_upper(key) if case_insensitive else key


def collision_key(rel: str) -> str:
    """Para saber si dos nombres del ordenador serían el mismo archivo en un disco que no
    distingue mayúsculas."""
    return _simple_upper(rel)


def invalid_reason(name: str) -> str | None:
    """Por qué un nombre no se puede escribir en exFAT/FAT/NTFS, o None si se puede."""
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return "el nombre no es UTF-8 válido"
    bad = sorted({ch for ch in name if ch in _FORBIDDEN or ord(ch) < 0x20})
    if bad:
        shown = " ".join(repr(ch)[1:-1] for ch in bad)
        return f"lleva caracteres que el disco no admite ({shown})"
    if name.endswith((" ", ".")):
        # El driver de exFAT de Linux quita los puntos del final (salvo con
        # «keep_last_dots»): el archivo aparecería con otro nombre y se volvería a copiar
        # en cada pasada.
        return "acaba en espacio o en punto, y el disco se lo quitaría"
    return None
