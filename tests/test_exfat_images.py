"""Sobre discos de verdad: un exFAT como el SSD y un origen que distingue mayúsculas, como
el ext4 de Linux. Se crean con `hdiutil` (sólo en el Mac) y se montan para la prueba.

Es lo más cerca de Debian que se puede llegar sin Debian: el driver de exFAT es el de macOS
y no el del kernel de Linux, así que lo que dependa del driver (los puntos del final, los
caracteres prohibidos) se comprueba por nuestras reglas y no por lo que haga el disco.
"""

import os
import shutil
import subprocess
import sys
import unicodedata
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from linbackup import names, rootmark, volumes
from linbackup.config import DiskProfile, FolderMode, SyncFolder
from linbackup.engine import SyncEngine
from linbackup.report import ChangeKind, ForeignRoot

pytestmark = [
    pytest.mark.imagenes,
    pytest.mark.skipif(sys.platform != "darwin" or shutil.which("hdiutil") is None,
                       reason="las imágenes de disco se crean con hdiutil, sólo en el Mac"),
]


def _attach(image: Path) -> Path:
    out = subprocess.run(["hdiutil", "attach", "-nobrowse", "-plist", str(image)],
                         capture_output=True, check=True).stdout
    import plistlib
    for entity in plistlib.loads(out)["system-entities"]:
        if "mount-point" in entity:
            return Path(entity["mount-point"])
    raise RuntimeError("la imagen no se montó")


def _make(tmp: Path, fs: str, label: str) -> Path:
    image = tmp / f"{label}.dmg"
    subprocess.run(["hdiutil", "create", "-quiet", "-size", "200m", "-fs", fs, "-volname", label,
                    "-type", "UDIF", str(image)], check=True)
    return _attach(image)


@pytest.fixture(scope="module")
def discos(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Path, Path]]:
    """(exFAT de destino, APFS que distingue mayúsculas de origen)."""
    tmp = tmp_path_factory.mktemp("imagenes")
    tag = uuid.uuid4().hex[:6].upper()
    exfat = _make(tmp, "ExFAT", f"LBEX{tag}")
    origen = _make(tmp, "Case-sensitive APFS", f"LBCS{tag}")
    try:
        yield exfat, origen
    finally:
        for mount in (exfat, origen):
            subprocess.run(["hdiutil", "detach", "-force", "-quiet", str(mount)], check=False)


@pytest.fixture
def lados(discos: tuple[Path, Path]) -> Iterator[tuple[Path, Path]]:
    """Una carpeta limpia a cada lado para cada prueba."""
    exfat, origen = discos
    name = uuid.uuid4().hex[:8]
    src = origen / name
    root = exfat / f"raiz-{name}"
    src.mkdir()
    yield src, root
    shutil.rmtree(src, ignore_errors=True)
    shutil.rmtree(root, ignore_errors=True)


def _perfil(src: Path, mode: FolderMode = FolderMode.SYNC) -> DiskProfile:
    d = DiskProfile(volume_uuid="de-mentira")
    d.folders = [SyncFolder(source=str(src), name="datos", mode=mode)]
    return d


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_se_reconoce_cada_disco(discos: tuple[Path, Path]) -> None:
    exfat, origen = discos
    assert names.filesystem_of(exfat).fstype == "exfat"
    assert names.filesystem_of(exfat).restricted_names
    assert names.is_case_insensitive(exfat, may_write=True)
    (origen / "Sonda").write_text("x")
    assert not names.is_case_insensitive(origen, may_write=False)
    (origen / "Sonda").unlink()


def test_volumen_por_uuid_y_raiz(discos: tuple[Path, Path]) -> None:
    exfat, _ = discos
    info = volumes.info_of(exfat)
    assert info is not None and info.uuid
    found = volumes.find(info.uuid)
    assert found is not None and found.mount_point == exfat
    profile = DiskProfile(volume_uuid=info.uuid, volume_name=info.name, root_folder="Mi-Backup")
    assert volumes.backup_root(profile) == exfat / "Mi-Backup"
    # Una raíz en blanco no deja el backup suelto en la raíz del disco.
    profile.root_folder = "  "
    assert volumes.backup_root(profile).name == "Backup-Linux"


def test_copia_a_exfat_y_segunda_pasada_quieta(lados: tuple[Path, Path]) -> None:
    src, root = lados
    _write(src / "nota.txt", "hola")
    _write(src / "Año nuevo/Cumpleaños de mamá.txt", "felicidades")
    _write(src / "hondo/a/b/c.txt", "3")
    r = SyncEngine().run(_perfil(src), root)
    assert r.copied == 3 and r.errors == [] and r.skipped == []
    # La fecha en exFAT tiene menos precisión: la tolerancia hace que no se recopie nada.
    r2 = SyncEngine().run(_perfil(src), root)
    assert (r2.copied, r2.updated, r2.retired) == (0, 0, 0)


def test_lo_que_exfat_no_admite_se_salta_y_no_se_retira(lados: tuple[Path, Path]) -> None:
    src, root = lados
    _write(src / "bien.txt", "sí")
    _write(src / "Foto.jpg", "la de verdad")
    _write(src / "foto.jpg", "otra que en exFAT sería la misma")
    _write(src / "10:30 reunión.txt", "dos puntos")
    _write(src / "acaba en punto.", "punto")
    r = SyncEngine().run(_perfil(src), root)
    assert r.errors == []
    assert r.copied == 2                         # bien.txt y Foto.jpg
    assert len(r.skipped) == 3
    assert (root / "datos/Foto.jpg").read_text() == "la de verdad"
    # Y en la siguiente pasada, nada se mueve: ni se recopia ni se retira.
    r2 = SyncEngine().run(_perfil(src), root)
    assert (r2.copied, r2.updated, r2.retired) == (0, 0, 0)
    assert len(r2.skipped) == 3


def test_cambio_de_mayusculas_en_exfat(lados: tuple[Path, Path]) -> None:
    src, root = lados
    _write(src / "bruno.html", "gastos")
    SyncEngine().run(_perfil(src), root)
    (src / "bruno.html").rename(src / "Bruno.html")
    r = SyncEngine().run(_perfil(src), root)
    assert r.copied + r.updated + r.retired == 0
    assert [c.kind for c in r.changes] == [ChangeKind.RENAMED]
    assert os.listdir(root / "datos") == ["Bruno.html"]


def test_tildes_escritas_por_el_mac(lados: tuple[Path, Path]) -> None:
    src, root = lados
    nfc = "canción.mp3"
    nfd = unicodedata.normalize("NFD", nfc)
    _write(src / nfc, "la misma")
    # En el disco, el mismo archivo con el nombre en NFD, como lo dejaría el Mac.
    _write(root / "datos" / nfd, "la misma")
    os.utime(root / "datos" / nfd, ns=(os.stat(src / nfc).st_mtime_ns,) * 2)
    r = SyncEngine().run(_perfil(src), root)
    assert r.errors == []
    assert r.copied + r.updated + r.retired == 0
    on_disk = os.listdir(root / "datos")
    assert len(on_disk) == 1 and unicodedata.normalize("NFC", on_disk[0]) == nfc


def test_mover_a_exfat(lados: tuple[Path, Path]) -> None:
    src, root = lados
    _write(root / "datos/informe.pdf", "el de marzo")
    _write(src / "informe.pdf", "el de agosto")
    _write(src / "2024/viejo.txt", "de 2024")
    r = SyncEngine().run(_perfil(src, FolderMode.MOVE), root)
    assert r.moved == 2 and r.errors == [] and r.unverified == []
    assert (root / "datos/informe.pdf").read_text() == "el de marzo"
    assert (root / "datos/informe-2.pdf").read_text() == "el de agosto"
    assert not (src / "informe.pdf").exists()
    assert (src / "2024").is_dir()                # el esqueleto se queda


def test_retirar_en_exfat(lados: tuple[Path, Path]) -> None:
    src, root = lados
    _write(src / "proyecto/uno.txt", "1")
    _write(src / "proyecto/sub/dos.txt", "2")
    SyncEngine().run(_perfil(src), root)
    shutil.rmtree(src / "proyecto")
    r = SyncEngine().run(_perfil(src), root)
    assert r.errors == []
    retired = list((root / "_Retirados").iterdir())
    assert len(retired) == 1
    assert (retired[0] / "datos/proyecto/sub/dos.txt").read_text() == "2"


def test_marca_de_la_raiz_en_el_disco(discos: tuple[Path, Path]) -> None:
    exfat, _ = discos
    # La raíz del Mac: con cosas y sin marca. LinBackup no la toca.
    mac_root = exfat / f"Backup-SSD-{uuid.uuid4().hex[:6]}"
    _write(mac_root / "Documentos/carta.txt", "del Mac")
    with pytest.raises(ForeignRoot):
        rootmark.check(mac_root, dry_run=False)
    # Una nueva se marca y se usa.
    mine = exfat / f"Backup-Linux-{uuid.uuid4().hex[:6]}"
    rootmark.check(mine, dry_run=False)
    assert rootmark.read_mark(mine) is not None
    shutil.rmtree(mac_root)
    shutil.rmtree(mine)
