"""Los discos montados, vistos como los ve la configuración: por UUID.

En Linux se leen con `findmnt` y `lsblk` (util-linux, de serie en Debian), sin D-Bus: para
saber qué hay montado y comprobar el UUID basta. Lo que sí necesitará udisks2 por D-Bus es
**enterarse al momento** de que se ha enchufado el disco y expulsarlo; eso llegará con la
bandeja (ver CLAUDE.md).

En el Mac (sólo para desarrollar y probar con imágenes de disco) se usa `diskutil`.

Ojo: el UUID de un exFAT no es el mismo en los dos sistemas. Linux da el número de serie
(`ABCD-1234`); macOS, uno largo inventado a partir de él.
"""

import json
import os
import plistlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import DEFAULT_ROOT, DiskProfile
from .report import NoDestination, WrongVolume


@dataclass(frozen=True)
class VolumeInfo:
    mount_point: Path       # cambia entre conexiones: no sirve para identificar el disco
    uuid: str               # esto sí, y es lo único que se guarda
    name: str
    fstype: str
    removable: bool
    is_boot: bool = False

    @property
    def looks_like_backup_disk(self) -> bool:
        """Lo que tiene sentido ofrecer como destino: nunca el disco del sistema."""
        return not self.is_boot


_SYSTEM_FS = {"proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2", "securityfs",
              "pstore", "efivarfs", "bpf", "debugfs", "tracefs", "configfs", "fusectl", "mqueue",
              "hugetlbfs", "autofs", "overlay", "squashfs", "ramfs", "binfmt_misc", "nsfs",
              "fuse.portal", "fuse.gvfsd-fuse"}


def _run(argv: list[str]) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


# --- Linux ----------------------------------------------------------------------------------


def _linux_mounted() -> list[VolumeInfo]:
    raw = _run(["findmnt", "-J", "-l", "-o", "TARGET,SOURCE,UUID,LABEL,FSTYPE"])
    try:
        rows = json.loads(raw).get("filesystems", []) if raw else []
    except ValueError:
        rows = []
    removable = _linux_removable()
    out = []
    for row in rows:
        uuid, target, fstype = row.get("uuid"), row.get("target"), row.get("fstype") or ""
        if not uuid or not target or fstype in _SYSTEM_FS:
            continue
        source = row.get("source") or ""
        out.append(VolumeInfo(mount_point=Path(target), uuid=uuid,
                              name=row.get("label") or Path(target).name or target,
                              fstype=fstype, removable=removable.get(source, False),
                              is_boot=target in ("/", "/boot", "/boot/efi", "/home")))
    return out


def _linux_removable() -> dict[str, bool]:
    raw = _run(["lsblk", "-J", "-l", "-o", "PATH,RM,HOTPLUG"])
    try:
        rows = json.loads(raw).get("blockdevices", []) if raw else []
    except ValueError:
        return {}
    return {r["path"]: bool(r.get("rm")) or bool(r.get("hotplug")) for r in rows if r.get("path")}


# --- macOS (sólo desarrollo) --------------------------------------------------------------


def _mac_info(mount_point: Path) -> VolumeInfo | None:
    raw = subprocess.run(["diskutil", "info", "-plist", str(mount_point)],
                         capture_output=True, timeout=15).stdout
    try:
        info = plistlib.loads(raw) if raw else {}
    except Exception:
        return None
    uuid = info.get("VolumeUUID") or ""
    if not uuid:
        return None
    return VolumeInfo(mount_point=mount_point, uuid=uuid,
                      name=info.get("VolumeName") or mount_point.name,
                      fstype=(info.get("FilesystemType") or "").lower(),
                      removable=bool(info.get("Ejectable") or info.get("Removable")),
                      is_boot=str(mount_point) == "/")


def _mac_mounted() -> list[VolumeInfo]:
    points = [Path("/")] + [Path("/Volumes") / n for n in sorted(os.listdir("/Volumes"))
                            if not n.startswith(".")]
    out = []
    for point in points:
        if point.is_symlink():          # /Volumes/Macintosh HD → /
            continue
        info = _mac_info(point)
        if info:
            out.append(info)
    return out


# --- Común ------------------------------------------------------------------------------


def mounted() -> list[VolumeInfo]:
    """Los volúmenes montados que tienen UUID. Sin UUID no hay forma de reconocer el disco
    la próxima vez, y todo el programa se apoya en eso."""
    vols = _mac_mounted() if sys.platform == "darwin" else _linux_mounted()
    return sorted(vols, key=lambda v: (v.is_boot, not v.removable, v.name.lower()))


def find(uuid: str) -> VolumeInfo | None:
    if not uuid:
        return None
    return next((v for v in mounted() if v.uuid == uuid), None)


def info_of(mount_point: Path) -> VolumeInfo | None:
    if sys.platform == "darwin":
        return _mac_info(mount_point)
    return next((v for v in _linux_mounted() if v.mount_point == mount_point), None)


def backup_root(profile: DiskProfile) -> Path:
    """La raíz del backup dentro del disco: `/media/bruno/SSD/Backup-Linux`.

    El UUID se comprueba **otra vez**, justo antes de devolver la ruta: entre que se vio el
    disco y ahora puede haberse desmontado y haber llegado otro al mismo sitio, y escribir
    ahí sería copiar en el disco de otro y retirar de él lo que no está aquí.
    """
    vol = find(profile.volume_uuid)
    if vol is None:
        raise NoDestination(profile.volume_name)
    now = info_of(vol.mount_point)
    if now is None or now.uuid != profile.volume_uuid:
        raise WrongVolume(profile.volume_name, now.name if now else "otro disco")
    folder = profile.root_folder.strip().strip("/")
    # Una raíz en blanco dejaría el backup en la raíz del disco, donde retirar lo que no
    # está aquí se llevaría todo lo demás.
    return vol.mount_point / (folder or DEFAULT_ROOT)
