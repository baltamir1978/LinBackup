"""Modo consola: `linbackup --sincronizar`, `--ensayo`, `--volumenes`…

Hasta que exista la bandeja, es la forma de usar LinBackup y de comprobar el motor sin
interfaz. La configuración se escribe a mano en ~/.config/linbackup/config.json
(`linbackup --ejemplo` enseña una).
"""

import json
import sys
from pathlib import Path

from . import __version__, history, rootmark, volumes
from .config import Config, DiskProfile, FolderMode, SyncFolder
from .engine import SyncEngine
from .history import HistoryEntry
from .report import ChangeKind, SyncError, SyncReport, human_bytes

USAGE = """\
LinBackup — copia tus carpetas a un SSD externo

Uso:
  linbackup --sincronizar [--disco <nombre o UUID>]   copiar a los discos configurados que estén puestos
  linbackup --ensayo [--disco …]                     lo mismo, sin escribir nada: cuenta lo que haría
  linbackup --volumenes                              discos montados, con su UUID
  linbackup --estado                                 configuración y problemas
  linbackup --historial                              las últimas sincronizaciones
  linbackup --ejemplo                                una configuración de ejemplo
  linbackup --version

Opciones de --sincronizar:
  --adoptar        usar una carpeta raíz que ya tiene cosas pero no lleva la marca de
                   LinBackup (sólo si de verdad es la de este equipo)
  --detalle        enseñar archivo a archivo qué se ha hecho

La configuración: ~/.config/linbackup/config.json
"""


def _err(text: str) -> None:
    print(text, file=sys.stderr)


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if argv else 2
    match argv[0]:
        case "--version":
            print(f"LinBackup {__version__}")
            return 0
        case "--volumenes":
            return run_volumes()
        case "--estado":
            return run_status()
        case "--historial":
            return run_history()
        case "--ejemplo":
            return run_example()
        case "--sincronizar" | "--ensayo":
            return run_sync(argv)
    _err(f"Opción desconocida: {argv[0]}")
    return 2


def _load_config() -> Config | None:
    try:
        return Config.load()
    except ValueError as error:
        _err(f"✗ {error}")
        return None


def run_volumes() -> int:
    vols = volumes.mounted()
    if not vols:
        print("No se ve ningún disco montado con UUID.")
        return 1
    for v in vols:
        flags = []
        if v.is_boot:
            flags.append("sistema")
        if v.removable:
            flags.append("extraíble")
        print(f"· {v.name}  [{v.fstype or '?'}]{'  (' + ', '.join(flags) + ')' if flags else ''}")
        print(f"    UUID {v.uuid}\n    en   {v.mount_point}")
    return 0


def run_status() -> int:
    config = _load_config()
    if config is None:
        return 1
    print(f"Configuración: {Config.file()}"
          f"{'' if Config.file().exists() else ' (no existe: linbackup --ejemplo)'}")
    if not config.disks:
        print("No hay ningún disco configurado.")
        return 0
    for disk in config.disks:
        vol = volumes.find(disk.volume_uuid)
        where = f"montado en {vol.mount_point}" if vol else "no está puesto"
        print(f"\n▸ {disk.volume_name or disk.volume_uuid}  ({where})")
        print(f"  raíz en el disco: {disk.root_folder}")
        for folder in disk.folders:
            mode = "mover" if folder.mode is FolderMode.MOVE else "sincronizar"
            off = "" if folder.enabled else "  [desactivada]"
            print(f"  · {folder.source} → {disk.destination(folder, Path(disk.root_folder))}"
                  f"  ({mode}){off}")
        for problem in config.problems_of(disk):
            print(f"  ⚠︎ {problem}")
    return 0


def run_history() -> int:
    entries = history.load()
    if not entries:
        print("Todavía no hay sincronizaciones.")
        return 0
    for e in entries[:20]:
        mark = "✓" if e.ok else ("■" if e.cancelled else "✗")
        print(f"{mark} {e.date.replace('T', ' ')}  {e.volume_name}: {e.summary}")
    return 0


def run_example() -> int:
    disk = DiskProfile(volume_uuid="ABCD-1234", volume_name="SSD")
    disk.folders = [SyncFolder(source="~/Documentos", name="Documentos"),
                    SyncFolder(source="~/Documentos/otros", name="otros", mode=FolderMode.MOVE)]
    data = Config(disks=[disk]).to_json()
    print(json.dumps(data, ensure_ascii=False, indent=2))
    print("\n# El UUID sale de: linbackup --volumenes", file=sys.stderr)
    return 0


def run_sync(argv: list[str]) -> int:
    dry_run = argv[0] == "--ensayo" or "--ensayo" in argv
    adopt = "--adoptar" in argv
    detail = "--detalle" in argv
    wanted = argv[argv.index("--disco") + 1] if "--disco" in argv and \
        argv.index("--disco") + 1 < len(argv) else None

    config = _load_config()
    if config is None:
        return 1
    disks = config.disks
    if wanted:
        disks = [d for d in disks if wanted in (d.volume_uuid, d.volume_name)]
        if not disks:
            _err(f"✗ No hay ningún disco configurado que se llame o sea «{wanted}».")
            return 2
    if not disks:
        _err("✗ No hay ningún disco configurado. Mira: linbackup --ejemplo")
        return 2

    status = 0
    ran = 0
    for disk in disks:
        label = disk.volume_name or disk.volume_uuid
        if volumes.find(disk.volume_uuid) is None:
            if wanted:
                _err(f"✗ El disco {label} no está puesto.")
                status = 1
            continue
        problems = config.problems_of(disk)
        if problems:
            _err(f"✗ {label}: " + " ".join(problems))
            status = 1
            continue
        print(f"▸ {label}{' (ensayo: no se escribe nada)' if dry_run else ''}")
        sys.stdout.flush()       # que el «✗» no adelante al nombre del disco
        try:
            root = volumes.backup_root(disk)
            rootmark.check(root, dry_run=dry_run, adopt=adopt)
            report = _run_engine(disk, root, dry_run)
        except SyncError as error:
            _err(f"✗ {error}")
            if not dry_run:
                history.record(HistoryEntry.from_failure(str(error), label))
            status = 1
            continue
        except KeyboardInterrupt:
            _err("\n✗ Detenido")
            return 130
        ran += 1
        _print_report(report, detail or dry_run)
        if not dry_run:
            history.record(HistoryEntry.from_report(report, label))
        if report.errors or report.cancelled:
            status = 1
    if ran == 0 and status == 0:
        print("Ninguno de los discos configurados está puesto.")
    return status


def _run_engine(disk: DiskProfile, root: Path, dry_run: bool) -> SyncReport:
    engine = SyncEngine()
    last = [0]

    def progress(done: int, total: int, current: str) -> None:
        if total and (done - last[0] >= 25 or done == total):
            last[0] = done
            sys.stdout.write(f"\r  {done}/{total}  {current[:60]:<60}")
            sys.stdout.flush()
        elif not total:
            sys.stdout.write(f"\r  {current[:70]:<70}")
            sys.stdout.flush()

    engine.on_progress = progress
    try:
        return engine.run(disk, root, dry_run=dry_run)
    except KeyboardInterrupt:
        engine.cancel()
        raise
    finally:
        sys.stdout.write("\r" + " " * 80 + "\r")
        sys.stdout.flush()


def _print_report(report: SyncReport, detail: bool) -> None:
    print(f"  {report.summary}  ·  {human_bytes(report.bytes_written)} en "
          f"{report.duration:.1f} s")
    if detail:
        symbols = {ChangeKind.NEW: "+", ChangeKind.UPDATED: "~", ChangeKind.MOVED: "→",
                   ChangeKind.RETIRED: "−", ChangeKind.RENAMED: "Aa"}
        for change in report.changes:
            print(f"    {symbols[change.kind]:>2} {change.path}")
        if report.changes_omitted:
            print(f"    … y {report.changes_omitted} más")
    for title, items in (("Saltados", report.skipped), ("Cambiaban mientras se copiaban "
                         "(se rehacen la próxima vez)", report.changed),
                         ("Copiados pero no movidos (no coincidían)", report.unverified),
                         ("Entraron con otro nombre", report.renamed),
                         ("Errores", report.errors)):
        if items:
            print(f"  {title}:")
            for item in items[:30]:
                print(f"    · {item}")
            if len(items) > 30:
                print(f"    … y {len(items) - 30} más")
