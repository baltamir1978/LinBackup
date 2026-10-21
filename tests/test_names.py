"""Nombres: lo que en el Mac no pasaba porque APFS y exFAT se parecían."""

import unicodedata
from pathlib import Path

from conftest import Sandbox
from linbackup import names
from linbackup.engine import Entry, SyncEngine
from linbackup.report import SyncReport


def test_nombres_que_exfat_no_admite() -> None:
    assert names.invalid_reason("normal.txt") is None
    assert names.invalid_reason("Año nuevo ☕.md") is None
    assert names.invalid_reason("10:30 reunión.txt") is not None
    assert names.invalid_reason("¿qué?.txt") is not None
    assert names.invalid_reason('dijo "hola".txt') is not None
    assert names.invalid_reason("acaba en punto.") is not None
    assert names.invalid_reason("acaba en espacio ") is not None
    assert names.invalid_reason("con\ttabulador") is not None
    # Un nombre con bytes que no son UTF-8 (en Linux, posible) llega con «surrogates».
    assert names.invalid_reason("malo\udcff.txt") is not None


def test_claves_de_emparejado() -> None:
    nfd = unicodedata.normalize("NFD", "canción.mp3")
    assert nfd != "canción.mp3"
    assert names.pairing_key(nfd, False) == names.pairing_key("canción.mp3", False)
    assert names.pairing_key("Foto.JPG", True) == names.pairing_key("foto.jpg", True)
    assert names.pairing_key("Foto.JPG", False) != names.pairing_key("foto.jpg", False)
    # exFAT compara letra a letra: «ß» no es «SS».
    assert names.pairing_key("straße", True) != names.pairing_key("STRASSE", True)


def _entries(*rels: str) -> dict[str, Entry]:
    return {r: Entry(r, r.endswith("/"), False, 1, 0) for r in rels}


def test_dos_que_en_el_disco_serian_uno(tmp_path: Path) -> None:
    # En ext4 conviven; en el exFAT del disco, no: se copia el primero y se avisa del otro.
    report = SyncReport()
    entries = {"Foto.jpg": Entry("Foto.jpg", False, False, 1, 0),
               "foto.jpg": Entry("foto.jpg", False, False, 1, 0),
               "otra.jpg": Entry("otra.jpg", False, False, 1, 0)}
    kept = SyncEngine()._admissible(entries, restricted=True, insensitive=True,
                                    folder_name="datos", report=report)
    assert sorted(kept) == ["Foto.jpg", "otra.jpg"]
    assert len(report.skipped) == 1 and "foto.jpg" in report.skipped[0]
    assert "mayúsculas" in report.skipped[0]


def test_carpetas_que_chocan_se_saltan_enteras() -> None:
    report = SyncReport()
    entries = {"Fotos": Entry("Fotos", True, False, 0, 0),
               "Fotos/a.jpg": Entry("Fotos/a.jpg", False, False, 1, 0),
               "fotos": Entry("fotos", True, False, 0, 0),
               "fotos/b.jpg": Entry("fotos/b.jpg", False, False, 1, 0)}
    kept = SyncEngine()._admissible(entries, restricted=True, insensitive=True,
                                    folder_name="datos", report=report)
    assert sorted(kept) == ["Fotos", "Fotos/a.jpg"]
    assert len(report.skipped) == 1          # la carpeta, no cada archivo


def test_nombres_prohibidos_se_saltan_con_lo_de_dentro() -> None:
    report = SyncReport()
    entries = {"bien.txt": Entry("bien.txt", False, False, 1, 0),
               "10:30.txt": Entry("10:30.txt", False, False, 1, 0),
               "carpeta?": Entry("carpeta?", True, False, 0, 0),
               "carpeta?/dentro.txt": Entry("carpeta?/dentro.txt", False, False, 1, 0)}
    kept = SyncEngine()._admissible(entries, restricted=True, insensitive=True,
                                    folder_name="datos", report=report)
    assert sorted(kept) == ["bien.txt"]
    assert len(report.skipped) == 2


def test_en_un_disco_linux_no_se_salta_nada() -> None:
    # A un ext4 se puede copiar «10:30.txt» y tener Foto.jpg y foto.jpg a la vez.
    report = SyncReport()
    entries = {"10:30.txt": Entry("10:30.txt", False, False, 1, 0),
               "Foto.jpg": Entry("Foto.jpg", False, False, 1, 0),
               "foto.jpg": Entry("foto.jpg", False, False, 1, 0)}
    kept = SyncEngine()._admissible(entries, restricted=False, insensitive=False,
                                    folder_name="datos", report=report)
    assert len(kept) == 3 and report.skipped == []


def test_tildes_del_mac_en_el_disco(sb: Sandbox) -> None:
    # El disco trae «canción.mp3» en NFD (como lo escribe el Mac); aquí está en NFC. Para
    # exFAT en Linux son dos nombres: sin emparejarlos se retiraría y se recopiaría.
    engine = SyncEngine()
    source = {"canción.mp3": Entry("canción.mp3", False, False, 10, 5_000_000_000)}
    nfd = unicodedata.normalize("NFD", "canción.mp3")
    dest = {nfd: Entry(nfd, False, False, 10, 5_000_000_000)}
    report = SyncReport()
    plan = engine._make_plan(source, dest, insensitive=False, report=report)
    kinds = [type(a).__name__ for a in plan]
    assert kinds == ["Rename"]                    # ni copia ni retirada
    assert report.unchanged == 1


def test_sistema_de_archivos_del_disco_de_pruebas(tmp_path: Path) -> None:
    info = names.filesystem_of(tmp_path / "no" / "existe")
    assert info.mount_point          # encuentra dónde está montado aunque no exista aún
