"""Andamiaje: una zona de pruebas nueva por test, con un «ordenador» y un «disco»."""

import os
from pathlib import Path

import pytest

from linbackup.config import DiskProfile, FolderMode, SyncFolder
from linbackup.engine import SyncEngine, today
from linbackup.report import SyncReport


class Sandbox:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.origen = root / "origen"
        self.disco = root / "disco"
        self.origen.mkdir(parents=True, exist_ok=True)
        self.disco.mkdir(parents=True, exist_ok=True)

    def write(self, rel: str, text: str, base: Path | None = None) -> Path:
        path = (base or self.origen) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def mkdir(self, rel: str, base: Path | None = None) -> None:
        ((base or self.origen) / rel).mkdir(parents=True, exist_ok=True)

    def exists(self, rel: str, base: Path | None = None) -> bool:
        return os.path.lexists((base or self.disco) / rel)

    def read(self, rel: str, base: Path | None = None) -> str | None:
        try:
            return ((base or self.disco) / rel).read_text(encoding="utf-8")
        except OSError:
            return None

    def config(self, keep_removed: bool = True, excludes: list[str] | None = None,
               retention: int = 90) -> DiskProfile:
        c = DiskProfile(volume_uuid="de-mentira")
        c.folders = [SyncFolder(source=str(self.origen), name="datos")]
        c.keep_removed = keep_removed
        c.removed_retention_days = retention
        if excludes is not None:
            c.excludes = excludes
        return c

    def config_mover(self) -> DiskProfile:
        c = self.config()
        c.folders = [SyncFolder(source=str(self.origen), name="datos", mode=FolderMode.MOVE)]
        return c

    def run(self, config: DiskProfile, dry_run: bool = False,
            root: Path | None = None) -> SyncReport:
        return SyncEngine().run(config, root or self.disco, dry_run=dry_run)

    def in_mac(self, rel: str) -> bool:
        return os.path.lexists(self.origen / rel)


@pytest.fixture
def sb(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


@pytest.fixture
def hoy() -> str:
    return today()


def set_mtime(path: Path, seconds_from_now: float) -> None:
    import time
    t = time.time() + seconds_from_now
    os.utime(path, (t, t))


def sparse(path: Path, size: int) -> None:
    """Un archivo «hueco»: se declara el tamaño y no se reserva nada hasta que se escriba.
    Copiarlo sí cuesta lo suyo, que es lo que hace falta para que dé tiempo a pararlo."""
    with open(path, "wb") as handle:
        handle.truncate(size)
