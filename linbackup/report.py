"""Qué se ha hecho en una sincronización, y por qué pudo no hacerse."""

from dataclasses import dataclass, field
from enum import Enum


class ChangeKind(Enum):
    NEW = "nuevo"
    UPDATED = "actualizado"
    MOVED = "movido"
    RETIRED = "retirado"
    # Le ha cambiado sólo el nombre en el disco: las mayúsculas o la forma Unicode
    # de las tildes. Ver `engine._square_names`.
    RENAMED = "renombrado"


@dataclass(frozen=True)
class Change:
    kind: ChangeKind
    path: str          # desde la raíz del backup: «Documentos/informe.pdf»
    bytes: int = 0


def plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def human_bytes(n: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    unit = 0
    while n >= 1000 and unit < len(units) - 1:
        n /= 1000
        unit += 1
    return f"{int(n)} B" if unit == 0 else f"{n:.1f} {units[unit]}".replace(".", ",")


@dataclass
class SyncReport:
    """Los números dicen *cuántos*; `changes`, *cuáles*.

    `changes` tiene tope: la primera copia de un disco son cientos de miles de archivos, y
    no tiene sentido guardarlos todos para una lista que nadie leerá hasta el final. Lo que
    no cabe se cuenta en `changes_omitted`.
    """

    MAX_CHANGES = 3000

    changes: list[Change] = field(default_factory=list)
    changes_omitted: int = 0

    copied: int = 0          # nuevos en el disco
    updated: int = 0         # ya estaban, pero habían cambiado
    retired: int = 0         # ya no están en el ordenador
    unchanged: int = 0
    moved: int = 0           # llevados al disco y borrados del ordenador (modo mover)
    # Copiados, pero el original **sigue en el ordenador** porque lo que llegó al disco no
    # coincidía con lo que salió. Nunca es un borrado a ciegas.
    unverified: list[str] = field(default_factory=list)
    # Ya había uno con ese nombre en el disco, con otro contenido, y el que llegaba entró
    # al lado como «-2».
    renamed: list[str] = field(default_factory=list)
    # Lo que no se ha copiado a propósito, con el motivo: nombres que el disco no admite,
    # dos archivos que en el disco serían el mismo…
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Estaban cambiando mientras se copiaban: la copia puede ser un a medias y se ha
    # marcado para rehacerla en la próxima pasada.
    changed: list[str] = field(default_factory=list)
    # Se borraron entre mirar y copiar. No es un fallo.
    vanished: list[str] = field(default_factory=list)
    bytes_written: int = 0
    purged: int = 0          # carpetas de _Retirados borradas por antigüedad
    duration: float = 0.0
    cancelled: bool = False

    def note(self, kind: ChangeKind, path: str, size: int = 0) -> None:
        if len(self.changes) >= self.MAX_CHANGES:
            self.changes_omitted += 1
        else:
            self.changes.append(Change(kind, path, size))

    def note_all(self, changes: list[Change]) -> None:
        for change in changes:
            self.note(change.kind, change.path, change.bytes)

    @property
    def touched(self) -> int:
        return self.copied + self.updated + self.retired + self.moved

    @property
    def summary(self) -> str:
        """Resumen de una línea para el registro y la bandeja."""
        if self.cancelled:
            done = self.copied + self.updated + self.moved
            return f"Detenido — {plural(done, 'archivo copiado', 'archivos copiados')} antes de parar"
        parts = []
        if self.copied:
            parts.append(plural(self.copied, "nuevo", "nuevos"))
        if self.updated:
            parts.append(plural(self.updated, "actualizado", "actualizados"))
        if self.moved:
            parts.append(plural(self.moved, "movido", "movidos"))
        if self.retired:
            parts.append(plural(self.retired, "retirado", "retirados"))
        if not parts:
            parts.append("sin cambios")
        if self.skipped:
            parts.append(plural(len(self.skipped), "saltado", "saltados"))
        if self.errors:
            parts.append(plural(len(self.errors), "con error", "con error"))
        return ", ".join(parts)


class SyncError(Exception):
    """Un motivo para no empezar, o para parar a mitad. El texto es el que ve el usuario."""


class NoDestination(SyncError):
    def __init__(self, disk_name: str) -> None:
        super().__init__(f"El disco {disk_name} no está montado." if disk_name
                         else "El disco de siempre no está montado.")


class WrongVolume(SyncError):
    def __init__(self, expected: str, found: str) -> None:
        super().__init__(f"Ese no es el disco de siempre: se esperaba {expected} y hay {found}. "
                         "No se ha tocado nada.")


class NotEnoughSpace(SyncError):
    def __init__(self, needed: int, free: int) -> None:
        super().__init__(f"No cabe: hacen falta {human_bytes(needed)} y quedan "
                         f"{human_bytes(free)} libres.")


class Disconnected(SyncError):
    def __init__(self) -> None:
        super().__init__("El disco se ha desconectado a mitad de la copia.")


class Nested(SyncError):
    pass


class ForeignRoot(SyncError):
    """La carpeta raíz del disco parece de otro equipo o de otra app (ver `rootmark`)."""


class Stopped(Exception):
    """Se ha pedido parar a mitad de un archivo. No es un fallo y no se apunta como error."""
