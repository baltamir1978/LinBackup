"""El motor decide qué se copia y, sobre todo, qué se retira del disco. Eso no se prueba a
ojo con el disco de verdad: se prueba aquí, sobre carpetas de mentira.

Son las pruebas de BackupSSD (macapp/tests/main.swift), traducidas caso a caso. Las de
Fotosync, Itusync e iCloud no tienen sentido en Linux y no están.
"""

import os
import threading
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

from conftest import Sandbox, set_mtime, sparse
from linbackup.config import DiskProfile, FolderMode, SyncFolder
from linbackup.engine import MTIME_TOLERANCE_NS, SyncEngine
from linbackup.report import ChangeKind, Nested, SyncReport

# --- Sincronizar ------------------------------------------------------------------------------


def test_copia_inicial(sb: Sandbox) -> None:
    sb.write("nota.txt", "hola")
    sb.write("sub/otro.txt", "adiós")
    sb.write("sub/hondo/tercero.txt", "3")
    r = sb.run(sb.config())
    assert r.copied == 3
    assert r.updated == 0
    assert r.errors == []
    assert sb.exists("datos/nota.txt")
    assert sb.exists("datos/sub/hondo/tercero.txt")       # respeta la jerarquía
    assert sb.read("datos/sub/otro.txt") == "adiós"


def test_segunda_pasada_sin_cambios(sb: Sandbox) -> None:
    sb.write("a.txt", "1")
    sb.write("b/c.txt", "2")
    sb.run(sb.config())
    r = sb.run(sb.config())
    assert (r.copied, r.updated, r.retired, r.bytes_written) == (0, 0, 0, 0)


def test_un_archivo_cambia(sb: Sandbox) -> None:
    sb.write("a.txt", "viejo")
    sb.write("b.txt", "igual")
    sb.run(sb.config())
    # Fecha muy posterior: dentro de la tolerancia de 2 s no se vería distinto.
    set_mtime(sb.write("a.txt", "nuevo y más largo"), 60)
    r = sb.run(sb.config())
    assert r.updated == 1 and r.copied == 0
    assert sb.read("datos/a.txt") == "nuevo y más largo"


def test_mismo_tamano_fecha_nueva(sb: Sandbox) -> None:
    sb.write("a.txt", "AAAA")
    sb.run(sb.config())
    set_mtime(sb.write("a.txt", "BBBB"), 120)        # mismo tamaño exacto
    r = sb.run(sb.config())
    assert r.updated == 1                             # no se fía sólo del tamaño
    assert sb.read("datos/a.txt") == "BBBB"


def test_borrar_aparta_no_pierde(sb: Sandbox, hoy: str) -> None:
    sb.write("importante.txt", "no me borres del todo")
    sb.write("queda.txt", "yo me quedo")
    sb.run(sb.config())
    (sb.origen / "importante.txt").unlink()
    r = sb.run(sb.config())
    assert r.retired == 1
    assert not sb.exists("datos/importante.txt")
    assert sb.read(f"_Retirados/{hoy}/datos/importante.txt") == "no me borres del todo"
    assert sb.exists("datos/queda.txt")


def test_borrar_carpeta_de_una_pieza(sb: Sandbox, hoy: str) -> None:
    sb.write("proyecto/uno.txt", "1")
    sb.write("proyecto/dos.txt", "2")
    sb.write("proyecto/sub/tres.txt", "3")
    sb.run(sb.config())
    import shutil
    shutil.rmtree(sb.origen / "proyecto")
    r = sb.run(sb.config())
    assert not sb.exists("datos/proyecto")
    assert sb.exists(f"_Retirados/{hoy}/datos/proyecto/uno.txt")
    assert sb.exists(f"_Retirados/{hoy}/datos/proyecto/sub/tres.txt")
    assert not sb.exists(f"_Retirados/{hoy}/datos/proyecto-2")     # sin partirla en dos
    assert r.errors == []


def test_sin_papelera(sb: Sandbox) -> None:
    sb.write("efimero.txt", "x")
    sb.run(sb.config(keep_removed=False))
    (sb.origen / "efimero.txt").unlink()
    r = sb.run(sb.config(keep_removed=False))
    assert r.retired == 1
    assert not sb.exists("datos/efimero.txt")
    assert not sb.exists("_Retirados")


def test_lo_excluido_no_se_copia(sb: Sandbox) -> None:
    sb.write("bueno.txt", "sí")
    sb.write(".DS_Store", "basura")
    sb.write("cosas.pyc", "basura")
    sb.write("node_modules/lib/x.js", "muchísima basura")
    sb.write("codigo/main.py", "sí")
    sb.write(".Trash-1000/files/borrado.txt", "papelera de un USB")
    r = sb.run(sb.config())
    assert sb.exists("datos/bueno.txt")
    assert sb.exists("datos/codigo/main.py")
    assert not sb.exists("datos/.DS_Store")
    assert not sb.exists("datos/cosas.pyc")
    assert not sb.exists("datos/node_modules")
    assert not sb.exists("datos/.Trash-1000")      # nuevo: la papelera de Linux
    assert r.copied == 2


def test_enlaces_se_recrean(sb: Sandbox) -> None:
    sb.write("real.txt", "contenido")
    os.symlink("real.txt", sb.origen / "atajo.txt")
    os.symlink("no-existe.txt", sb.origen / "roto.txt")
    r = sb.run(sb.config())
    assert os.readlink(sb.disco / "datos/atajo.txt") == "real.txt"
    assert os.path.lexists(sb.disco / "datos/roto.txt")     # el roto también, sin error
    assert r.errors == []


def test_enlace_a_carpeta(sb: Sandbox) -> None:
    sb.write("real/dentro.txt", "contenido")
    os.symlink("real", sb.origen / "atajo")
    r = sb.run(sb.config())
    assert r.errors == []
    assert os.readlink(sb.disco / "datos/atajo") == "real"
    assert sb.exists("datos/real/dentro.txt")


def test_retirar_enlace_a_carpeta(sb: Sandbox, hoy: str) -> None:
    # Si al retirarlo se le tratara como carpeta, se llevaría lo que hay al otro lado.
    sb.write("real/dentro.txt", "contenido")
    os.symlink("real", sb.origen / "atajo")
    sb.run(sb.config())
    (sb.origen / "atajo").unlink()
    r = sb.run(sb.config())
    assert r.errors == []
    assert sb.read("datos/real/dentro.txt") == "contenido"
    assert not os.path.lexists(sb.disco / "datos/atajo")
    assert not sb.exists(f"_Retirados/{hoy}/datos/real/dentro.txt")


def test_nombres_raros(sb: Sandbox) -> None:
    sb.write("Año nuevo/Cumpleaños de mamá.txt", "felicidades")
    sb.write("café ☕.md", "solo")
    r = sb.run(sb.config())
    assert r.errors == []
    assert sb.exists("datos/Año nuevo/Cumpleaños de mamá.txt")
    assert sb.exists("datos/café ☕.md")


def test_ensayo(sb: Sandbox) -> None:
    sb.write("a.txt", "1")
    sb.write("b.txt", "2")
    r = sb.run(sb.config(), dry_run=True)
    assert r.copied == 2
    assert not sb.exists("datos")


def test_ensayo_sin_raiz(sb: Sandbox) -> None:
    sb.write("a.txt", "1")
    sb.disco.rmdir()
    r = sb.run(sb.config(), dry_run=True)
    assert r.errors == []
    assert r.copied == 1
    assert not sb.disco.exists()


def test_espacio_libre() -> None:
    free = SyncEngine.free_space(Path("/"))
    assert free is not None and free > 0
    # Una ruta que aún no existe se mide en la carpeta más cercana que sí.
    assert SyncEngine.free_space(Path("/tmp/no/existe/aun")) is not None


def test_intruso_se_retira(sb: Sandbox, hoy: str) -> None:
    sb.write("mio.txt", "1")
    sb.run(sb.config())
    sb.write("datos/intruso.txt", "yo no vengo del ordenador", base=sb.disco)
    r = sb.run(sb.config())
    assert r.retired == 1
    assert sb.exists(f"_Retirados/{hoy}/datos/intruso.txt")


def test_retirar_dos_veces_el_mismo_dia(sb: Sandbox, hoy: str) -> None:
    sb.write("vaiviene.txt", "primera versión")
    sb.run(sb.config())
    (sb.origen / "vaiviene.txt").unlink()
    sb.run(sb.config())
    sb.write("vaiviene.txt", "segunda versión")
    sb.run(sb.config())
    (sb.origen / "vaiviene.txt").unlink()
    sb.run(sb.config())
    assert sb.read(f"_Retirados/{hoy}/datos/vaiviene.txt") == "primera versión"
    assert sb.read(f"_Retirados/{hoy}/datos/vaiviene-2.txt") == "segunda versión"


def test_purga(sb: Sandbox, hoy: str) -> None:
    sb.write("a.txt", "1")
    sb.run(sb.config())
    vieja = (date.today() - timedelta(days=200)).isoformat()
    sb.write(f"_Retirados/{vieja}/datos/antiguo.txt", "de otra era", base=sb.disco)
    sb.write(f"_Retirados/{hoy}/datos/reciente.txt", "de hoy", base=sb.disco)
    r = sb.run(sb.config(retention=90))
    assert r.purged == 1
    assert not sb.exists(f"_Retirados/{vieja}")
    assert sb.exists(f"_Retirados/{hoy}/datos/reciente.txt")


def test_retencion_cero(sb: Sandbox) -> None:
    sb.write("a.txt", "1")
    vieja = (date.today() - timedelta(days=3000)).isoformat()
    sb.write(f"_Retirados/{vieja}/datos/x.txt", "reliquia", base=sb.disco)
    r = sb.run(sb.config(retention=0))
    assert r.purged == 0
    assert sb.exists(f"_Retirados/{vieja}/datos/x.txt")


def test_destino_dentro_del_origen(sb: Sandbox) -> None:
    sb.write("a.txt", "1")
    c = sb.config()
    c.folders = [SyncFolder(source=str(sb.root), name="todo")]
    with pytest.raises(Nested):
        sb.run(c)


def test_cancelar_a_mitad(sb: Sandbox) -> None:
    for i in range(200):
        sb.write(f"f{i}.txt", "x" * 500)
    engine = SyncEngine()
    engine.on_progress = lambda done, total, cur: engine.cancel() if done > 20 else None
    r = engine.run(sb.config(), sb.disco)
    assert r.cancelled
    assert 0 < r.copied < 200


def test_detener_archivo_grande(sb: Sandbox) -> None:
    # 2 GB huecos: no ocupan, pero copiarlos cuesta. Hay que parar a mitad, no al acabar.
    sparse(sb.origen / "pelicula.bin", 2 * 1024 ** 3)
    engine = SyncEngine()
    threading.Timer(0.05, engine.cancel).start()
    t0 = time.monotonic()
    r = engine.run(sb.config(), sb.disco)
    took = time.monotonic() - t0
    assert r.cancelled
    assert took < 3, f"tardó {took:.1f} s"
    assert r.errors == []                              # parar no es un error
    assert not sb.exists("datos/pelicula.bin")         # el a medias no queda con nombre bueno
    leftovers = os.listdir(sb.disco / "datos")
    assert not any(n.endswith(".bssd-parcial") for n in leftovers)


def test_archivo_que_cambia_mientras_se_copia(sb: Sandbox) -> None:
    path = sb.origen / "diario.txt"
    sparse(path, 2 * 1024 ** 3)

    def save() -> None:
        with open(path, "r+b") as handle:
            handle.write(b"el usuario acaba de guardar")

    threading.Timer(0.05, save).start()
    r = SyncEngine().run(sb.config(), sb.disco)
    assert any("diario.txt" in c for c in r.changed)
    # Lo que importa: la copia no puede quedar sellada con la fecha nueva.
    mo = os.stat(path).st_mtime_ns
    md = os.stat(sb.disco / "datos/diario.txt").st_mtime_ns
    assert abs(mo - md) > MTIME_TOLERANCE_NS
    # Con el archivo ya quieto, la siguiente lo copia bien y la tercera no hace nada.
    path.write_text("el usuario acaba de guardar")
    r2 = sb.run(sb.config())
    assert r2.errors == []
    r3 = sb.run(sb.config())
    assert r3.copied + r3.updated == 0


def test_archivo_que_desaparece(sb: Sandbox) -> None:
    sb.write("queda.txt", "aquí sigo")
    efimero = sb.origen / "temporal.txt"
    sparse(efimero, 300 * 1024 ** 2)
    threading.Timer(0.02, lambda: efimero.unlink(missing_ok=True)).start()
    r = SyncEngine().run(sb.config(), sb.disco)
    assert r.errors == []
    assert sb.exists("datos/queda.txt")


def test_carpeta_vacia(sb: Sandbox) -> None:
    sb.mkdir("carpeta vacía")
    sb.write("a.txt", "1")
    sb.run(sb.config())
    assert (sb.disco / "datos/carpeta vacía").is_dir()


def test_dos_carpetas_mismo_destino(sb: Sandbox) -> None:
    sb.mkdir("uno/docs")
    sb.mkdir("dos/docs")
    c = sb.config()
    c.folders = [SyncFolder(source=str(sb.origen / "uno/docs")),
                 SyncFolder(source=str(sb.origen / "dos/docs"))]
    assert any("el mismo sitio" in p for p in c.problems())


def test_carpeta_desaparecida(sb: Sandbox) -> None:
    c = sb.config()
    c.folders = [SyncFolder(source=str(sb.origen / "fantasma"))]
    assert any("ya no existe" in p for p in c.problems())


# --- Mover -----------------------------------------------------------------------------------


def test_mover_basico(sb: Sandbox) -> None:
    sb.write("a.txt", "uno")
    sb.write("2024/b.txt", "dos")
    r = sb.run(sb.config_mover())
    assert r.moved == 2 and r.retired == 0
    assert sb.exists("datos/a.txt")
    assert sb.read("datos/2024/b.txt") == "dos"
    assert not sb.in_mac("a.txt") and not sb.in_mac("2024/b.txt")


def test_mover_esqueleto(sb: Sandbox) -> None:
    sb.write("2024/enero/a.txt", "x")
    sb.run(sb.config_mover())
    assert (sb.origen / "2024/enero").is_dir()
    assert os.listdir(sb.origen / "2024/enero") == []


def test_mover_acumula(sb: Sandbox) -> None:
    sb.write("datos/archivado.txt", "de antes", base=sb.disco)
    sb.write("nuevo.txt", "de ahora")
    r = sb.run(sb.config_mover())
    assert sb.read("datos/archivado.txt") == "de antes"
    assert sb.exists("datos/nuevo.txt")
    assert r.retired == 0
    assert not sb.exists("_Retirados")


def test_mover_colision(sb: Sandbox) -> None:
    sb.write("datos/informe.pdf", "el de marzo", base=sb.disco)
    sb.write("informe.pdf", "el de agosto")
    r = sb.run(sb.config_mover())
    assert sb.read("datos/informe.pdf") == "el de marzo"
    assert sb.read("datos/informe-2.pdf") == "el de agosto"
    assert r.moved == 1 and len(r.renamed) == 1
    assert not sb.in_mac("informe.pdf")


def test_mover_identico(sb: Sandbox) -> None:
    sb.write("datos/nota.txt", "lo mismo", base=sb.disco)
    sb.write("nota.txt", "lo mismo")
    r = sb.run(sb.config_mover())
    assert not sb.exists("datos/nota-2.txt")
    assert sb.read("datos/nota.txt") == "lo mismo"
    assert not sb.in_mac("nota.txt")
    assert r.moved == 1 and r.renamed == []


def test_mover_excluido(sb: Sandbox) -> None:
    sb.write("bueno.txt", "sí")
    sb.write(".DS_Store", "basura")
    sb.write("node_modules/lib.js", "no")
    sb.run(sb.config_mover())
    assert sb.exists("datos/bueno.txt")
    assert not sb.exists("datos/.DS_Store")
    assert sb.in_mac(".DS_Store")                     # lo que no se copia, no se borra
    assert sb.in_mac("node_modules/lib.js")


def test_mover_ensayo(sb: Sandbox) -> None:
    sb.write("a.txt", "uno")
    r = sb.run(sb.config_mover(), dry_run=True)
    assert r.moved == 1
    assert sb.in_mac("a.txt")
    assert not sb.exists("datos/a.txt")


def _anidada(sb: Sandbox) -> DiskProfile:
    c = sb.config()
    c.folders = [SyncFolder(source=str(sb.origen / "documentos"), name="Documentos"),
                 SyncFolder(source=str(sb.origen / "documentos/otros"), name="otros",
                            mode=FolderMode.MOVE)]
    return c


def test_mover_dentro_de_sincronizar(sb: Sandbox) -> None:
    sb.write("documentos/carta.txt", "hola")
    sb.write("documentos/otros/viejo.pdf", "de 2024")
    c = _anidada(sb)
    assert c.problems() == []
    r = sb.run(c)
    assert sb.exists("Documentos/carta.txt")
    assert sb.exists("Documentos/otros/viejo.pdf")
    assert not sb.in_mac("documentos/otros/viejo.pdf")
    assert sb.in_mac("documentos/carta.txt")
    assert r.retired == 0
    # La segunda es la peligrosa: «Documentos» ve en el disco un «otros» lleno de cosas que
    # ya no están en el ordenador. Si no se lo saltara, lo retiraría entero.
    r2 = sb.run(c)
    assert sb.exists("Documentos/otros/viejo.pdf")
    assert r2.retired == 0
    assert not sb.exists("_Retirados")


def test_mover_desactivada(sb: Sandbox) -> None:
    sb.write("documentos/otros/viejo.pdf", "de 2024")
    c = _anidada(sb)
    sb.run(c)
    assert sb.exists("Documentos/otros/viejo.pdf")
    c.folders[1].enabled = False          # «no la toques», nunca «trágatela desde fuera»
    r = sb.run(c)
    assert sb.exists("Documentos/otros/viejo.pdf")
    assert r.retired == 0


def test_destinos_anidados(sb: Sandbox) -> None:
    sb.mkdir("documentos/otros/hondo")
    c = sb.config()
    docs = SyncFolder(source=str(sb.origen / "documentos"), name="Documentos")
    otros = SyncFolder(source=str(sb.origen / "documentos/otros"), name="otros",
                       mode=FolderMode.MOVE)
    hondo = SyncFolder(source=str(sb.origen / "documentos/otros/hondo"), name="hondo")
    c.folders = [docs, otros, hondo]
    d = Path("/D")
    assert c.destination(docs, d) == Path("/D/Documentos")
    assert c.destination(otros, d) == Path("/D/Documentos/otros")
    assert c.destination(hondo, d) == Path("/D/Documentos/otros/hondo")
    assert c.nested_relative_paths(docs) == {"otros"}
    assert c.nested_relative_paths(otros) == {"hondo"}
    assert c.nested_relative_paths(hondo) == set()


def test_destinos_cruzados(sb: Sandbox) -> None:
    sb.mkdir("uno")
    sb.mkdir("dos")
    c = sb.config()
    raro = SyncFolder(source=str(sb.origen / "uno"), name="dos/uno")
    c.folders = [raro, SyncFolder(source=str(sb.origen / "dos"), name="dos")]
    p = c.problems()
    assert any("no vale como nombre" in x or "cae dentro" in x for x in p)


# --- Archivos de sólo lectura (lo que en el Mac era el candado `uchg`) ---------------------


def test_solo_lectura_se_copia_y_se_actualiza(sb: Sandbox, hoy: str) -> None:
    # Git deja sus objetos en sólo lectura. En el Mac el problema era la bandera `uchg`;
    # en Linux, que una copia de sólo lectura no se pudiera sustituir ni retirar.
    f = sb.write("acta.txt", "versión 1")
    f.chmod(0o444)
    r = sb.run(sb.config())
    assert r.errors == [] and r.copied == 1
    f.chmod(0o644)
    set_mtime(sb.write("acta.txt", "versión 2 mucho más larga"), 60)
    f.chmod(0o444)
    r2 = sb.run(sb.config())
    assert r2.errors == []
    assert sb.read("datos/acta.txt") == "versión 2 mucho más larga"
    f.chmod(0o644)
    f.unlink()
    r3 = sb.run(sb.config())
    assert r3.errors == [] and r3.retired == 1
    assert not sb.exists("datos/acta.txt")
    leftovers = os.listdir(sb.disco / "datos")
    assert not any(n.endswith(".bssd-parcial") for n in leftovers)


# --- Varios discos y el detalle --------------------------------------------------------------


def test_dos_discos_mismas_carpetas(sb: Sandbox) -> None:
    sb.write("informe.txt", "el mismo archivo para los dos")
    sb.write("hondo/nota.md", "y uno más adentro")
    disco2 = sb.root / "disco2"
    disco2.mkdir()
    a, b = sb.config(), sb.config()
    a.volume_uuid, b.volume_uuid = "AAA", "BBB"
    ra = sb.run(a)
    rb = sb.run(b, root=disco2)
    assert ra.copied == 2 and rb.copied == 2
    assert ra.retired == 0 and rb.retired == 0
    assert (disco2 / "datos/informe.txt").exists()
    ra2 = sb.run(a)
    assert ra2.retired == 0 and ra2.copied + ra2.updated == 0


def test_informe_nuevos(sb: Sandbox) -> None:
    sb.write("informe.txt", "hola")
    sb.write("hondo/nota.md", "más adentro")
    r = sb.run(sb.config())
    assert len(r.changes) == 2
    assert all(c.kind is ChangeKind.NEW for c in r.changes)
    assert sorted(c.path for c in r.changes) == ["datos/hondo/nota.md", "datos/informe.txt"]
    assert all(c.bytes > 0 for c in r.changes)


def test_informe_cambios_y_retirados(sb: Sandbox) -> None:
    sb.write("igual.txt", "no me toques")
    sb.write("cambia.txt", "versión 1")
    sb.write("se-va.txt", "adiós")
    sb.run(sb.config())
    set_mtime(sb.write("cambia.txt", "versión 2, que es más larga"), 60)
    (sb.origen / "se-va.txt").unlink()
    r = sb.run(sb.config())
    kinds = {c.kind: [x.path for x in r.changes if x.kind is c.kind] for c in r.changes}
    assert kinds[ChangeKind.UPDATED] == ["datos/cambia.txt"]
    assert kinds[ChangeKind.RETIRED] == ["datos/se-va.txt"]
    assert ChangeKind.NEW not in kinds
    assert len(r.changes) == 2


def test_informe_ensayo(sb: Sandbox) -> None:
    sb.write("uno.txt", "1")
    sb.write("dos.txt", "2")
    r = sb.run(sb.config(), dry_run=True)
    assert len(r.changes) == 2 and all(c.kind is ChangeKind.NEW for c in r.changes)
    assert not sb.exists("datos/uno.txt")


def test_informe_mover(sb: Sandbox) -> None:
    sb.write("factura.pdf", "contenido")
    r = sb.run(sb.config_mover())
    assert [c.kind for c in r.changes] == [ChangeKind.MOVED]
    assert r.changes[0].path == "datos/factura.pdf"


def test_informe_con_tope(sb: Sandbox) -> None:
    for i in range(1, SyncReport.MAX_CHANGES + 2):
        sb.write(f"f{i}.txt", "x")
    r = sb.run(sb.config())
    assert len(r.changes) == SyncReport.MAX_CHANGES
    assert r.changes_omitted == 1
    assert r.copied == SyncReport.MAX_CHANGES + 1


# --- Mayúsculas (en el Mac de pruebas, como en el exFAT: no las distingue) ------------------


def test_mayuscula_de_un_archivo(sb: Sandbox) -> None:
    mac = sb.write("bruno.html", "gastos")
    sb.run(sb.config())
    mac.rename(sb.origen / "Bruno.html")
    ensayo = sb.run(sb.config(), dry_run=True)
    assert [c.kind for c in ensayo.changes] == [ChangeKind.RENAMED]
    r = sb.run(sb.config())
    assert r.copied + r.updated == 0 and r.retired == 0
    assert [c.path for c in r.changes] == ["datos/Bruno.html"]
    assert sb.read("datos/Bruno.html") == "gastos"
    assert os.listdir(sb.disco / "datos") == ["Bruno.html"]
    assert not sb.exists("_Retirados")
    assert sb.run(sb.config()).changes == []


def test_mayuscula_de_una_carpeta(sb: Sandbox) -> None:
    sb.write("Fotos/a.jpg", "a")
    sb.write("Fotos/Viaje/b.jpg", "b")
    sb.run(sb.config())
    (sb.origen / "Fotos").rename(sb.origen / "fotos")
    (sb.origen / "fotos/Viaje").rename(sb.origen / "fotos/VIAJE")
    sb.write("fotos/VIAJE/c.jpg", "c")
    r = sb.run(sb.config())
    assert r.copied == 1 and r.updated == 0 and r.retired == 0 and r.errors == []
    assert os.listdir(sb.disco / "datos") == ["fotos"]
    assert set(os.listdir(sb.disco / "datos/fotos")) == {"a.jpg", "VIAJE"}
    assert sb.read("datos/fotos/VIAJE/b.jpg") == "b"
    assert sb.read("datos/fotos/VIAJE/c.jpg") == "c"


# --- Lo nuevo de LinBackup ----------------------------------------------------------------


def test_trozo_a_medias_se_limpia(sb: Sandbox) -> None:
    # Un `.bssd-parcial` que quedó de un corte no es de nadie: se borra, no se retira.
    sb.write("a.txt", "1")
    sb.run(sb.config())
    sb.write("datos/.a.txt.bssd-parcial", "medio archivo", base=sb.disco)
    r = sb.run(sb.config())
    assert r.retired == 0
    assert not sb.exists("datos/.a.txt.bssd-parcial")
    assert not sb.exists("_Retirados")


def test_permisos_se_conservan(sb: Sandbox) -> None:
    script = sb.write("lanzar.sh", "#!/bin/sh\necho hola\n")
    script.chmod(0o755)
    sb.run(sb.config())
    assert os.stat(sb.disco / "datos/lanzar.sh").st_mode & 0o111
