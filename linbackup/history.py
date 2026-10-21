"""El historial: lo que pasó en cada sincronización, para que «¿esto se copió?» no dependa
de que la app siga abierta desde entonces. ~/.local/state/linbackup/historial.json."""

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .report import SyncReport, plural

LIMIT = 100       # unos meses, sin que el archivo crezca sin fin


@dataclass
class HistoryEntry:
    date: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    volume_name: str = ""
    copied: int = 0
    updated: int = 0
    retired: int = 0
    moved: int = 0
    errors: list[str] = field(default_factory=list)
    skipped: int = 0
    changed: int = 0          # estaban en uso y se volverán a copiar
    unverified: int = 0       # copiados pero **no** borrados del ordenador
    bytes: int = 0
    duration: float = 0.0
    cancelled: bool = False
    failure: str | None = None    # si no llegó a empezar: por qué
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    @classmethod
    def from_report(cls, report: SyncReport, volume: str) -> "HistoryEntry":
        return cls(volume_name=volume, copied=report.copied, updated=report.updated,
                   retired=report.retired, moved=report.moved, errors=list(report.errors),
                   skipped=len(report.skipped), changed=len(report.changed),
                   unverified=len(report.unverified), bytes=report.bytes_written,
                   duration=report.duration, cancelled=report.cancelled)

    @classmethod
    def from_failure(cls, failure: str, volume: str) -> "HistoryEntry":
        return cls(volume_name=volume, failure=failure)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "HistoryEntry":
        """Clave a clave: una clave nueva o rara no tira la lectura de todo el archivo (en
        BackupSSD, eso dejaba el historial en blanco en cada arranque, sin avisar)."""
        entry = cls()
        defaults = asdict(entry)
        for key, default in defaults.items():
            value = data.get(key)
            if key == "failure":
                entry.failure = value if isinstance(value, str) else None
            elif isinstance(default, bool):
                if isinstance(value, bool):
                    setattr(entry, key, value)
            elif isinstance(default, (int, float)):
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    setattr(entry, key, type(default)(value))
            elif isinstance(default, list):
                if isinstance(value, list):
                    setattr(entry, key, [v for v in value if isinstance(v, str)])
            elif isinstance(value, str):
                setattr(entry, key, value)
        return entry

    @property
    def ok(self) -> bool:
        return self.failure is None and not self.errors and not self.cancelled

    @property
    def summary(self) -> str:
        if self.failure:
            return self.failure
        if self.cancelled:
            done = self.copied + self.updated + self.moved
            return f"Detenido — {plural(done, 'archivo copiado', 'archivos copiados')} antes de parar"
        parts = []
        for n, one, many in ((self.copied, "nuevo", "nuevos"),
                             (self.updated, "actualizado", "actualizados"),
                             (self.moved, "movido", "movidos"),
                             (self.retired, "retirado", "retirados")):
            if n:
                parts.append(plural(n, one, many))
        if not parts:
            parts.append("sin cambios")
        if self.changed:
            parts.append(plural(self.changed, "estaba en uso", "estaban en uso"))
        if self.unverified:
            parts.append(plural(self.unverified, "sin mover", "sin mover"))
        if self.skipped:
            parts.append(plural(self.skipped, "saltado", "saltados"))
        if self.errors:
            parts.append(plural(len(self.errors), "con error", "con error"))
        return ", ".join(parts)


def directory() -> Path:
    base = os.environ.get("XDG_STATE_HOME", "")
    root = Path(base) if base and os.path.isabs(base) else Path.home() / ".local/state"
    return root / "linbackup"


def file() -> Path:
    return directory() / "historial.json"


def broken_file() -> Path:
    return directory() / "historial-ilegible.json"


def load() -> list[HistoryEntry]:
    try:
        data = json.loads(file().read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("no es una lista")
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        # Ilegible: se aparta en vez de dejar que la próxima sincronización lo pise.
        try:
            os.replace(file(), broken_file())
        except OSError:
            pass
        return []
    return [HistoryEntry.from_json(d) for d in data if isinstance(d, dict)]


def save(entries: list[HistoryEntry]) -> None:
    directory().mkdir(parents=True, exist_ok=True)
    temporary = file().with_name("historial.json.nuevo")
    temporary.write_text(json.dumps([asdict(e) for e in entries[:LIMIT]], ensure_ascii=False,
                                    indent=2), encoding="utf-8")
    os.replace(temporary, file())


def record(entry: HistoryEntry) -> None:
    save([entry, *load()])
