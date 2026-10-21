"""Qué se copia, a qué disco y con qué reglas.

Vive en ~/.config/linbackup/config.json, en el mismo formato que el de BackupSSD
(~/.config/backup-ssd/config.json en el Mac): un archivo que se puede abrir y corregir con
un editor. **No se puede copiar tal cual de un equipo a otro**: macOS y Linux no dan el
mismo UUID para un mismo exFAT (macOS se inventa uno largo a partir del número de serie;
Linux da el número de serie, `ABCD-1234`).
"""

import json
import os
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

DEFAULT_ROOT = "Backup-Linux"

DEFAULT_EXCLUDES = [
    ".DS_Store", ".Trash", ".Trash-*", ".Spotlight-V100", ".fseventsd", ".TemporaryItems",
    "node_modules", ".build", ".pio", "__pycache__", "*.pyc",
    "build", "DerivedData", ".venv", "venv",
]


class FolderMode(Enum):
    # El disco queda igual que el ordenador: lo que se borra aquí se retira allí.
    SYNC = "sync"
    # Lo que hay en el ordenador se lleva al disco y se borra de aquí. Se **añade** a lo
    # que ya hubiera en el destino y no se retira nada nunca.
    MOVE = "move"


def _resolved(path: str) -> str:
    return os.path.realpath(path).rstrip("/") or "/"


@dataclass
class SyncFolder:
    """Un origen y el nombre de su copia dentro del disco.

    El nombre va aparte porque dos carpetas distintas pueden llamarse igual
    (`~/Proyectos/docs` y `~/Trabajo/docs`). Sólo manda cuando la carpeta no cuelga de otra
    configurada: si cuelga, el destino sale de la de fuera (ver `DiskProfile.destination`).
    """

    source: str
    name: str = ""
    enabled: bool = True
    mode: FolderMode = FolderMode.SYNC
    id: str = field(default_factory=lambda: str(uuid.uuid4()).upper())

    def __post_init__(self) -> None:
        self.source = os.path.expanduser(self.source)
        if not self.name:
            self.name = os.path.basename(self.source.rstrip("/"))

    @property
    def resolved_source(self) -> str:
        """Con los enlaces resueltos: la forma en que se compara con otros orígenes."""
        return _resolved(self.source)

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "source": self.source, "name": self.name,
                "enabled": self.enabled, "mode": self.mode.value}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "SyncFolder":
        source = data.get("source")
        if not isinstance(source, str):
            raise ValueError("una carpeta sin «source»")
        try:
            # Una configuración de antes del modo mover no trae la clave: lo que había
            # entonces era sincronizar, que es lo que no borra nada del ordenador.
            mode = FolderMode(data.get("mode", "sync"))
        except ValueError:
            mode = FolderMode.SYNC
        folder = cls(source=source, name=str(data.get("name") or ""),
                     enabled=data.get("enabled", True) is not False, mode=mode)
        if isinstance(data.get("id"), str):
            folder.id = data["id"]
        return folder


@dataclass
class DiskProfile:
    """Todo lo que hay que saber de **un** disco."""

    # El UUID del volumen **es la clave de todo**: por el nombre sería temerario, porque
    # dos discos pueden llamarse «Backup». Copiar al disco equivocado y, peor, *retirar*
    # del disco equivocado lo que no está en el ordenador, es el peor fallo posible.
    volume_uuid: str = ""
    volume_name: str = ""          # sólo para enseñarlo
    # Todo cuelga de aquí, para que el disco pueda tener otras cosas que no se tocan.
    # Distinta de la de BackupSSD («Backup-SSD») a propósito: cada equipo, su raíz.
    root_folder: str = DEFAULT_ROOT
    folders: list[SyncFolder] = field(default_factory=list)
    auto_sync_on_mount: bool = True
    # Lo que se borra del ordenador se aparta a `_Retirados/AAAA-MM-DD/` en el disco.
    keep_removed: bool = True
    # Días que se guarda lo retirado. A 0, no se purga nunca.
    removed_retention_days: int = 90
    # Se comparan con el **nombre**, no con la ruta, y admiten `*` al principio o al final.
    excludes: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    id: str = field(default_factory=lambda: str(uuid.uuid4()).upper())

    # --- Persistencia: cada clave por separado y con su valor por omisión ----------------
    # Una sola clave ausente o rara no puede tirar la lectura entera: la app arrancaría sin
    # discos ni carpetas y parecería que se ha perdido todo (lección de BackupSSD).

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "volumeUUID": self.volume_uuid, "volumeName": self.volume_name,
                "rootFolder": self.root_folder, "folders": [f.to_json() for f in self.folders],
                "autoSyncOnMount": self.auto_sync_on_mount, "keepRemoved": self.keep_removed,
                "removedRetentionDays": self.removed_retention_days, "excludes": self.excludes}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "DiskProfile":
        disk = cls()
        if isinstance(data.get("id"), str):
            disk.id = data["id"]
        for key, attr in (("volumeUUID", "volume_uuid"), ("volumeName", "volume_name"),
                          ("rootFolder", "root_folder")):
            if isinstance(data.get(key), str):
                setattr(disk, attr, data[key])
        for key, attr in (("autoSyncOnMount", "auto_sync_on_mount"),
                          ("keepRemoved", "keep_removed")):
            if isinstance(data.get(key), bool):
                setattr(disk, attr, data[key])
        days = data.get("removedRetentionDays")
        if isinstance(days, int) and not isinstance(days, bool) and days >= 0:
            disk.removed_retention_days = days
        excludes = data.get("excludes")
        if isinstance(excludes, list):
            disk.excludes = [e for e in excludes if isinstance(e, str)]
        folders = data.get("folders")
        if isinstance(folders, list):
            disk.folders = []
            for raw in folders:
                if isinstance(raw, dict):
                    try:
                        disk.folders.append(SyncFolder.from_json(raw))
                    except ValueError:
                        continue
        return disk

    # --- Quién cuelga de quién -------------------------------------------------------

    def parent(self, folder: SyncFolder) -> SyncFolder | None:
        """La carpeta configurada de la que cuelga `folder`, la más específica.

        Se miran **todas**, también las desactivadas: si desactivar `Documentos/otros` la
        sacara de la lista, `Documentos` la recorrería, no encontraría en el ordenador lo
        que se movió al disco y lo retiraría entero. Desactivar quiere decir «no la toques»,
        nunca «trágatela desde fuera».
        """
        mine = folder.resolved_source
        best: SyncFolder | None = None
        for other in self.folders:
            if other.id == folder.id:
                continue
            theirs = other.resolved_source
            if mine.startswith(theirs + "/") and (
                    best is None or len(theirs) > len(best.resolved_source)):
                best = other
        return best

    def is_descendant(self, a: SyncFolder, b: SyncFolder) -> bool:
        return a.resolved_source.startswith(b.resolved_source + "/")

    def destination(self, folder: SyncFolder, root: Path) -> Path:
        """Dónde acaba una carpeta dentro del disco.

        Si cuelga de otra configurada, ocupa su sitio natural dentro de aquella
        (`~/Documentos/otros` → `<raíz>/Documentos/otros`): la jerarquía del disco es la del
        ordenador porque se calcula de ella, y así no hay forma de anidar mal dos destinos.
        """
        parent = self.parent(folder)
        if parent is None:
            return root / folder.name
        rel = folder.resolved_source[len(parent.resolved_source) + 1:]
        return self.destination(parent, root) / rel

    def nested_relative_paths(self, folder: SyncFolder) -> set[str]:
        """Rutas relativas que el recorrido de `folder` se salta: las de las carpetas
        configuradas que cuelgan directamente de ella. A **los dos lados**: en el destino es
        lo que evita retirar entero lo que el modo mover guardó allí."""
        base = folder.resolved_source
        return {other.resolved_source[len(base) + 1:] for other in self.folders
                if other.id != folder.id and (p := self.parent(other)) is not None
                and p.id == folder.id}

    # --- Comprobaciones --------------------------------------------------------------

    def problems(self) -> list[str]:
        """Lo que impide sincronizar. Vacío quiere decir que se puede empezar."""
        out: list[str] = []
        if not self.volume_uuid:
            out.append("No hay ningún disco elegido todavía.")
        active = [f for f in self.folders if f.enabled]
        if not active:
            out.append("No hay ninguna carpeta que copiar.")
        if not self.root_folder.strip() or "/" in self.root_folder.strip("/") \
                or self.root_folder.strip() in (".", ".."):
            out.append(f"«{self.root_folder}» no vale como carpeta raíz del disco.")
        for f in active:
            if not os.path.exists(f.source):
                out.append(f"«{f.name}»: la carpeta {f.source} ya no existe.")
            elif not os.path.isdir(f.source):
                out.append(f"«{f.name}»: {f.source} no es una carpeta.")
            if "/" in f.name or f.name in (".", "..", ""):
                out.append(f"«{f.name}» no vale como nombre de carpeta en el disco.")
        # Destinos ya resueltos y sin distinguir mayúsculas: en un exFAT «Trabajo» y
        # «trabajo» son la misma carpeta, y la segunda retiraría lo de la primera.
        root = Path("/")
        seen: dict[str, SyncFolder] = {}
        for f in active:
            d = str(self.destination(f, root)).lower()
            if d in seen:
                out.append(f"«{seen[d].name}» y «{f.name}» quieren el mismo sitio en el disco.")
            else:
                seen[d] = f
        # Un destino dentro de otro sólo vale si en el ordenador también es así.
        for f in active:
            df = str(self.destination(f, root)).lower()
            for g in active:
                if g.id == f.id:
                    continue
                dg = str(self.destination(g, root)).lower()
                if df.startswith(dg + "/") and not self.is_descendant(f, g):
                    out.append(f"El destino de «{f.name}» cae dentro del de «{g.name}» sin estar "
                               "dentro de ella en el ordenador.")
        return out


@dataclass
class Config:
    disks: list[DiskProfile] = field(default_factory=list)
    appearance: str = "system"

    @staticmethod
    def directory() -> Path:
        base = os.environ.get("XDG_CONFIG_HOME", "")
        root = Path(base) if base and os.path.isabs(base) else Path.home() / ".config"
        return root / "linbackup"

    @classmethod
    def file(cls) -> Path:
        return cls.directory() / "config.json"

    def to_json(self) -> dict[str, Any]:
        return {"disks": [d.to_json() for d in self.disks], "appearance": self.appearance}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Config":
        config = cls()
        if isinstance(data.get("appearance"), str):
            config.appearance = data["appearance"]
        disks = data.get("disks")
        if isinstance(disks, list):
            config.disks = [DiskProfile.from_json(d) for d in disks if isinstance(d, dict)]
        elif "volumeUUID" in data or "folders" in data:
            # El formato de cuando BackupSSD sólo sabía de un disco: el disco en la raíz.
            single = DiskProfile.from_json(data)
            config.disks = [single] if single.volume_uuid or single.folders else []
        return config

    @classmethod
    def load(cls) -> "Config":
        try:
            with cls.file().open(encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError) as error:
            # Un archivo que no se entiende no se pisa: quien llama decide qué hacer.
            raise ValueError(f"No se puede leer {cls.file()}: {error}") from error
        return cls.from_json(data) if isinstance(data, dict) else cls()

    def save(self) -> None:
        """A un archivo aparte y luego se renombra: si se va la luz a media escritura, el
        archivo viejo sigue entero."""
        path = self.file()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".nuevo")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(self.to_json(), handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(temporary, path)

    def disk(self, volume_uuid: str) -> DiskProfile | None:
        if not volume_uuid:
            return None
        return next((d for d in self.disks if d.volume_uuid == volume_uuid), None)

    def problems_of(self, disk: DiskProfile) -> list[str]:
        out = disk.problems()
        if disk.volume_uuid and any(d.id != disk.id and d.volume_uuid == disk.volume_uuid
                                    for d in self.disks):
            out.append("Hay otra configuración para este mismo disco.")
        return out
