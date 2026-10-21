"""La configuración, el historial y la marca de la raíz: archivos que se escriben y se
vuelven a leer, y de esos hay que probar siempre el viaje de ida y vuelta."""

import json
from pathlib import Path

import pytest

from linbackup import history, rootmark
from linbackup.config import Config, DiskProfile, FolderMode, SyncFolder
from linbackup.history import HistoryEntry
from linbackup.report import ForeignRoot, SyncReport


@pytest.fixture(autouse=True)
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


# --- Configuración ------------------------------------------------------------------------


def test_formato_de_un_solo_disco() -> None:
    # Tal cual lo escribía BackupSSD cuando sólo sabía de un disco.
    v1 = {
        "appearance": "dark", "autoSyncOnMount": True, "excludes": [".DS_Store", "node_modules"],
        "folders": [
            {"enabled": True, "mode": "sync", "name": "Documents", "source": "/home/x/Documents"},
            {"enabled": True, "mode": "move", "name": "Casero", "source": "/home/x/Casero"},
        ],
        "keepRemoved": True, "removedRetentionDays": 45, "rootFolder": "Backup",
        "skipUndownloadediCloud": True, "volumeName": "Extreme SSD", "volumeUUID": "ABCD-1234",
    }
    cfg = Config.from_json(v1)
    assert len(cfg.disks) == 1
    d = cfg.disks[0]
    assert (d.volume_uuid, d.volume_name, d.root_folder) == ("ABCD-1234", "Extreme SSD", "Backup")
    assert len(d.folders) == 2 and d.folders[0].name == "Documents"
    assert d.folders[1].mode is FolderMode.MOVE
    assert d.removed_retention_days == 45
    assert d.excludes == [".DS_Store", "node_modules"]
    assert cfg.appearance == "dark"


def test_configuracion_nueva_no_es_migracion() -> None:
    assert Config.from_json({"appearance": "system"}).disks == []


def test_formato_de_varios_discos() -> None:
    cfg = Config.from_json({"disks": [
        {"volumeUUID": "AAA", "volumeName": "Uno", "rootFolder": "Backup", "folders": []},
        {"volumeUUID": "BBB", "volumeName": "Dos", "rootFolder": "Copias", "folders": []},
    ]})
    assert len(cfg.disks) == 2
    found = cfg.disk("BBB")
    assert found is not None and found.root_folder == "Copias"
    assert cfg.disks[0].id != cfg.disks[1].id


def test_ida_y_vuelta() -> None:
    uno = DiskProfile(volume_uuid="AAA", volume_name="Uno", root_folder="Backup")
    uno.folders = [SyncFolder(source="/tmp/a", name="a")]
    dos = DiskProfile(volume_uuid="BBB", volume_name="Dos", removed_retention_days=7)
    cfg = Config(disks=[uno, dos], appearance="light")
    cfg.save()
    leida = Config.load()
    assert len(leida.disks) == 2
    assert leida.disks[0].id == uno.id
    found = leida.disk("BBB")
    assert found is not None and found.removed_retention_days == 7
    assert leida.appearance == "light"


def test_una_clave_rara_no_tira_lo_demas() -> None:
    cfg = Config.from_json({"disks": [{"volumeUUID": "AAA", "removedRetentionDays": "mucho",
                                       "keepRemoved": "sí", "folders": [{"source": "/x"},
                                                                        {"sin": "source"}]}]})
    d = cfg.disks[0]
    assert d.volume_uuid == "AAA"
    assert d.removed_retention_days == 90 and d.keep_removed is True
    assert [f.source for f in d.folders] == ["/x"]


def test_un_archivo_ilegible_no_se_pisa() -> None:
    Config.file().parent.mkdir(parents=True)
    Config.file().write_text("{ esto no es json")
    with pytest.raises(ValueError):
        Config.load()
    assert Config.file().read_text() == "{ esto no es json"


def test_dos_configuraciones_para_el_mismo_disco(tmp_path: Path) -> None:
    uno = DiskProfile(volume_uuid="AAA")
    uno.folders = [SyncFolder(source=str(tmp_path), name="tmp")]
    dos = DiskProfile(volume_uuid="AAA", folders=list(uno.folders))
    cfg = Config(disks=[uno, dos])
    assert any("mismo disco" in p for p in cfg.problems_of(uno))
    cfg.disks = [uno]
    assert cfg.problems_of(uno) == []


def test_problemas_de_un_disco_no_manchan_al_otro(tmp_path: Path) -> None:
    bueno = DiskProfile(volume_uuid="AAA", folders=[SyncFolder(source=str(tmp_path), name="t")])
    malo = DiskProfile(volume_uuid="BBB", folders=[SyncFolder(source="/no/existe", name="f")])
    cfg = Config(disks=[bueno, malo])
    assert cfg.problems_of(bueno) == []
    assert cfg.problems_of(malo)


def test_raiz_que_no_vale(tmp_path: Path) -> None:
    d = DiskProfile(volume_uuid="AAA", root_folder="  ",
                    folders=[SyncFolder(source=str(tmp_path), name="t")])
    assert any("carpeta raíz" in p for p in d.problems())
    d.root_folder = "a/b"
    assert any("carpeta raíz" in p for p in d.problems())


def test_raiz_por_omision_distinta_de_backupssd() -> None:
    # Cada equipo, su raíz: la de BackupSSD es «Backup-SSD».
    assert DiskProfile().root_folder != "Backup-SSD"


# --- Historial ------------------------------------------------------------------------------


def test_historial_ida_y_vuelta() -> None:
    r = SyncReport(copied=3, updated=1, bytes_written=4096)
    entries = [HistoryEntry.from_report(r, "Extreme SSD"),
               HistoryEntry.from_failure("El disco Extreme SSD no está montado.", "Extreme SSD")]
    history.save(entries)
    leidas = history.load()
    assert len(leidas) == 2
    assert leidas[0].copied == 3 and leidas[0].volume_name == "Extreme SSD"
    assert leidas[1].failure == "El disco Extreme SSD no está montado."
    assert leidas[0].date == entries[0].date


def test_historial_con_claves_nuevas_o_raras() -> None:
    history.directory().mkdir(parents=True)
    history.file().write_text(json.dumps([{"copied": 2, "inventada": True, "moved": "x"}]))
    leidas = history.load()
    assert leidas[0].copied == 2 and leidas[0].moved == 0


def test_historial_ilegible_se_aparta() -> None:
    history.directory().mkdir(parents=True)
    history.file().write_text("basura")
    assert history.load() == []
    assert history.broken_file().read_text() == "basura"     # no se pierde sin rastro


def test_historial_con_tope() -> None:
    history.save([HistoryEntry(copied=i) for i in range(history.LIMIT + 10)])
    assert len(history.load()) == history.LIMIT


# --- Marca de la raíz --------------------------------------------------------------------


def test_raiz_nueva_se_marca(tmp_path: Path) -> None:
    root = tmp_path / "Backup-Linux"
    rootmark.check(root, dry_run=False)
    mark = rootmark.read_mark(root)
    assert mark is not None and mark["app"] == "LinBackup"
    rootmark.check(root, dry_run=False)          # la segunda vez, es suya


def test_ensayo_no_marca(tmp_path: Path) -> None:
    root = tmp_path / "Backup-Linux"
    rootmark.check(root, dry_run=True)
    assert not root.exists()


def test_raiz_de_backupssd_se_rechaza(tmp_path: Path) -> None:
    # Así está la raíz del Mac: con cosas y sin marca de LinBackup.
    root = tmp_path / "Backup-SSD"
    (root / "Documentos").mkdir(parents=True)
    (root / "Documentos/carta.txt").write_text("del Mac")
    with pytest.raises(ForeignRoot, match="BackupSSD"):
        rootmark.check(root, dry_run=False)
    assert rootmark.read_mark(root) is None       # y no se ha tocado


def test_raiz_de_otro_equipo_se_rechaza(tmp_path: Path) -> None:
    root = tmp_path / "Backup-Linux"
    root.mkdir()
    (root / rootmark.MARK_FILE).write_text(json.dumps(
        {"app": "LinBackup", "equipo": "portatil", "machine_id": "otro-equipo"}))
    (root / "algo").write_text("x")
    with pytest.raises(ForeignRoot, match="portatil"):
        rootmark.check(root, dry_run=False)


def test_adoptar_una_raiz(tmp_path: Path) -> None:
    root = tmp_path / "Backup-Linux"
    root.mkdir()
    (root / "algo").write_text("x")
    rootmark.check(root, dry_run=False, adopt=True)
    rootmark.check(root, dry_run=False)           # ya es de este equipo
