"""El motor: compara el ordenador con el disco y deja el disco igual que el ordenador.

No depende de GTK ni de D-Bus a propósito: así se prueba desde la terminal en segundos
(tests/), que es la única forma sensata de fiarse de un programa que borra archivos.

En una frase: se hace una lista de lo que hay a cada lado, se compara, se decide qué hacer
con cada archivo, y sólo entonces se toca el disco. Planificar primero permite saber cuánto
espacio hace falta antes de escribir el primer byte, y da un progreso que significa algo.

Es la traducción del `SyncEngine.swift` de BackupSSD; los comentarios que explican el
porqué vienen de allí. Lo nuevo de Linux está en `_square_names` y `_collisions`.
"""

import os
import shutil
import stat
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from . import names
from .config import DiskProfile, FolderMode, SyncFolder
from .report import (Change, ChangeKind, Disconnected, Nested, NotEnoughSpace, Stopped,
                     SyncReport)

# Las fechas se dan por iguales si no se separan más de dos segundos. No es dejadez, es
# exFAT: un archivo recién copiado vuelve «distinto» comparado con el original. Sin esta
# tolerancia, cada sincronización recopiaría el disco entero.
MTIME_TOLERANCE_NS = 2_000_000_000

# Cuántos archivos se copian a la vez. Medido en BackupSSD: 1 → 4,73 s, 4 → 2,49 s,
# 8 → 3,55 s. Pasado ese punto sólo se consigue que el disco vaya y venga.
CONCURRENT_COPIES = 4

# A partir de este tamaño se copia a trozos, para poder parar a mitad: «detener y expulsar»
# no puede esperar a que acabe una película de 20 GB.
STREAMING_THRESHOLD = 64 * 1024 * 1024
CHUNK_SIZE = 4 * 1024 * 1024

PARTIAL_SUFFIX = ".bssd-parcial"     # el mismo que BackupSSD: el disco se lee igual
RETIRED_DIR = "_Retirados"


@dataclass(frozen=True)
class Entry:
    rel: str
    is_dir: bool
    is_symlink: bool
    size: int
    mtime_ns: int
    # A dónde apunta, si es un enlace: para no recrear en cada pasada los que no cambian.
    link_target: str | None = None


# --- Lo que hay que hacer ----------------------------------------------------------------


@dataclass(frozen=True)
class Copy:
    rel: str
    size: int
    mtime_ns: int
    is_new: bool


@dataclass(frozen=True)
class MakeDir:
    rel: str


@dataclass(frozen=True)
class Link:
    rel: str             # enlace simbólico: se recrea, no se sigue


@dataclass(frozen=True)
class Retire:
    rel: str
    is_dir: bool


@dataclass(frozen=True)
class Move:
    """Llevar al disco y borrar del ordenador, sólo después de comprobar lo que llegó."""
    rel: str
    size: int
    mtime_ns: int
    is_symlink: bool


@dataclass(frozen=True)
class Rename:
    """Cambiar en el disco el nombre de algo que ya está, porque en el ordenador se llama
    igual salvo por las mayúsculas o por la forma de las tildes."""
    old: str
    new: str


Action = Copy | MakeDir | Link | Retire | Move | Rename


def depth(rel: str) -> int:
    return rel.count("/") + 1


class ExcludeMatcher:
    """Compara nombres —no rutas— con patrones con `*` al principio o al final."""

    def __init__(self, patterns: list[str]) -> None:
        self.exact: set[str] = set()
        self.prefixes: list[str] = []
        self.suffixes: list[str] = []
        for raw in patterns:
            pattern = raw.strip()
            if not pattern:
                continue
            if pattern.startswith("*") and len(pattern) > 1:
                self.suffixes.append(pattern[1:])
            elif pattern.endswith("*") and len(pattern) > 1:
                self.prefixes.append(pattern[:-1])
            else:
                self.exact.add(pattern)

    def matches(self, name: str) -> bool:
        return (name in self.exact or any(name.endswith(s) for s in self.suffixes)
                or any(name.startswith(p) for p in self.prefixes))


@dataclass
class ScanResult:
    entries: dict[str, Entry]
    errors: list[str]


def today() -> str:
    return date.today().isoformat()


class SyncEngine:
    def __init__(self) -> None:
        self._cancelled = threading.Event()
        # Se llaman desde el hilo del motor: quien los reciba salta al suyo.
        self.on_progress: Callable[[int, int, str], None] | None = None
        self.on_log: Callable[[str], None] | None = None

    def cancel(self) -> None:
        self._cancelled.set()

    def reset(self) -> None:
        self._cancelled.clear()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def _progress(self, done: int, total: int, current: str) -> None:
        if self.on_progress:
            self.on_progress(done, total, current)

    # --- Ejecución -------------------------------------------------------------------

    def run(self, config: DiskProfile, root: Path, dry_run: bool = False) -> SyncReport:
        """`root`: la carpeta raíz dentro del disco, ya comprobada (ver `volumes`)."""
        started = time.monotonic()
        report = SyncReport()

        # La raíz se crea sólo si se va a copiar de verdad. Que no exista es lo normal la
        # primera vez, y no es motivo para no dejar hacer un ensayo; crearla en un ensayo
        # sería escribir en el disco justo cuando se ha pedido que no.
        if not root.exists() and not dry_run:
            root.mkdir(parents=True)

        folders = [f for f in config.folders if f.enabled]
        matcher = ExcludeMatcher(config.excludes)

        # --- 1. Planificar todo antes de tocar nada --------------------------------
        plans: list[tuple[SyncFolder, list[Action]]] = []
        bytes_needed = 0
        total_actions = 0
        for folder in folders:
            if self.cancelled:
                report.cancelled = True
                return report
            dest = config.destination(folder, root)
            self._assert_not_nested(folder.source, dest)
            self._progress(0, 0, f"Comparando «{folder.name}»…")
            source = Path(folder.source)
            skip = config.nested_relative_paths(folder)

            # Lo que sabe el disco de nombres. Si el destino no existe aún, se mira el
            # disco desde la carpeta más cercana que sí exista.
            probe_dir = dest if dest.is_dir() else (root if root.is_dir() else root.parent)
            fs = names.filesystem_of(probe_dir)
            insensitive = names.is_case_insensitive(probe_dir, may_write=not dry_run)

            if folder.mode is FolderMode.MOVE:
                # Aquí no se mira el disco: se lleva todo lo que haya en el ordenador y lo
                # que ya esté en el disco se queda. Las coincidencias de nombre se resuelven
                # al escribir, que es cuando se sabe de verdad lo que hay al otro lado.
                src = self._scan(source, matcher, skip)
                report.errors += src.errors
                entries = self._admissible(src.entries, fs.restricted_names, insensitive,
                                           folder.name, report)
                actions = self._make_move_plan(entries)
            else:
                has_dest = dest.exists()
                # Los dos lados a la vez: son dos aparatos distintos, y mientras uno contesta
                # el otro está parado.
                with ThreadPoolExecutor(max_workers=2) as pool:
                    src_future = pool.submit(self._scan, source, matcher, skip)
                    dst_future = pool.submit(self._scan, dest, matcher, skip, True) \
                        if has_dest else None
                    src = src_future.result()
                    dst = dst_future.result() if dst_future else ScanResult({}, [])
                report.errors += src.errors + dst.errors
                entries = self._admissible(src.entries, fs.restricted_names, insensitive,
                                           folder.name, report)
                actions = self._make_plan(entries, dst.entries, insensitive, report)

            for action in actions:
                if isinstance(action, (Copy, Move)):
                    bytes_needed += action.size
            total_actions += len(actions)
            plans.append((folder, actions))

        # --- 2. ¿Cabe? --------------------------------------------------------------
        # Con el total, aunque parte sustituya a lo que ya está: quedarse sin espacio a
        # mitad es peor que no empezar. Un 5 % de margen.
        if not dry_run and bytes_needed > 0:
            free = self.free_space(root)
            if free is not None:
                needed = bytes_needed + bytes_needed // 20
                if needed > free:
                    raise NotEnoughSpace(needed, free)

        if dry_run:
            for folder, actions in plans:
                for action in actions:
                    label = f"{folder.name}/{getattr(action, 'rel', '')}"
                    match action:
                        case Copy(rel, size, _, is_new):
                            if is_new:
                                report.copied += 1
                            else:
                                report.updated += 1
                            report.note(ChangeKind.NEW if is_new else ChangeKind.UPDATED,
                                        label, size)
                        case Move(rel, size, _, _):
                            report.moved += 1
                            report.note(ChangeKind.MOVED, label, size)
                        case Retire():
                            report.retired += 1
                            report.note(ChangeKind.RETIRED, label)
                        case Rename(_, new):
                            report.note(ChangeKind.RENAMED, f"{folder.name}/{new}")
            report.duration = time.monotonic() - started
            return report

        # --- 3. Ejecutar --------------------------------------------------------------
        retired_dir = root / RETIRED_DIR / today()
        done = 0
        for folder, actions in plans:
            dest = config.destination(folder, root)
            source = Path(folder.source)
            renames = [a for a in actions if isinstance(a, Rename)]
            dirs = [a for a in actions if isinstance(a, MakeDir)]
            copies = [a for a in actions if isinstance(a, (Copy, Link))]
            moves = [a for a in actions if isinstance(a, Move)]
            retires = [a for a in actions if isinstance(a, Retire)]

            # Los renombres antes que nada: el resto del plan ya cuenta con que lo del disco
            # se llama como en el ordenador. Si uno falla, lo de después sigue siendo seguro.
            for r in renames:
                if self.cancelled:
                    break
                try:
                    os.rename(dest / r.old, dest / r.new)
                    report.note(ChangeKind.RENAMED, f"{folder.name}/{r.new}")
                except OSError as error:
                    report.errors.append(f"No se pudo renombrar {folder.name}/{r.old}: "
                                         f"{error.strerror or error}")
            done += len(renames)

            # Las carpetas, en orden: el padre antes que el hijo.
            for d in dirs:
                if self.cancelled:
                    break
                try:
                    (dest / d.rel).mkdir(parents=True, exist_ok=True)
                except OSError as error:
                    report.errors.append(f"No se pudo crear {d.rel}: {error.strerror or error}")
            done += len(dirs)

            if not self.cancelled:
                done = self._copy_in_parallel(copies, source, dest, folder.name, root, done,
                                              total_actions, report)
            if not self.cancelled:
                done = self._move_in_parallel(moves, source, dest, folder.name, root, done,
                                              total_actions, report)

            # Las retiradas, de una en una y de dentro hacia fuera.
            for ret in retires:
                if self.cancelled:
                    break
                done += 1
                if done % 50 == 0 and not root.exists():
                    raise Disconnected()
                victim = dest / ret.rel
                self._progress(done, total_actions, ret.rel)
                try:
                    if config.keep_removed:
                        self._retire(victim, retired_dir / folder.name / ret.rel)
                    else:
                        _remove(victim)
                    report.retired += 1
                    report.note(ChangeKind.RETIRED, f"{folder.name}/{ret.rel}")
                except OSError as error:
                    # Una carpeta que ya no está —se fue con su padre— no es un error.
                    if os.path.lexists(victim):
                        report.errors.append(f"Al retirar {folder.name}/{ret.rel}: "
                                             f"{error.strerror or error}")
                    elif ret.is_dir:
                        report.retired += 1

            if self.cancelled:
                report.cancelled = True
                break

        # --- 4. Purga de lo retirado hace mucho -------------------------------------
        if config.keep_removed and config.removed_retention_days > 0 and not report.cancelled:
            report.purged = self._purge_old_retired(root, config.removed_retention_days, report)

        report.duration = time.monotonic() - started
        return report

    # --- Copiar ----------------------------------------------------------------------

    def _copy_in_parallel(self, actions: list[Copy | Link], source: Path, dest: Path,
                          folder_name: str, root: Path, done: int, total: int,
                          report: SyncReport) -> int:
        """Reparto por demanda —cada hilo coge el siguiente— y no en trozos iguales: un trozo
        con los cuatro vídeos tardaría más que los otros tres juntos."""
        if not actions:
            return done
        lock = threading.Lock()
        state = {"next": 0, "done": done, "disconnected": False}
        changes: list[Change] = []

        def worker() -> None:
            while True:
                with lock:
                    i = state["next"]
                    state["next"] = i + 1
                    if i >= len(actions) or state["disconnected"] or self.cancelled:
                        return
                action = actions[i]
                rel = action.rel
                label = f"{folder_name}/{rel}"
                error: str | None = None
                vanished = changed = False
                change: Change | None = None
                written = 0
                if isinstance(action, Copy):
                    src = source / rel
                    try:
                        changed = self._copy_atomically(src, dest / rel, action.size,
                                                        action.mtime_ns)
                        written = action.size
                        change = Change(ChangeKind.NEW if action.is_new else ChangeKind.UPDATED,
                                        label, action.size)
                    except Stopped:
                        pass          # parar no es un fallo del que informar
                    except OSError as e:
                        if self.cancelled:
                            pass
                        elif not os.path.lexists(src):
                            # Se ha borrado entre mirar y copiar: temporales de cualquier
                            # programa que esté trabajando. No es un fallo del backup.
                            vanished = True
                        else:
                            error = f"{label}: {e.strerror or e}"
                else:
                    try:
                        self._copy_symlink(source / rel, dest / rel)
                        # Un enlace recreado no tiene tamaño, pero hay que poder verlo.
                        change = Change(ChangeKind.NEW, label)
                    except OSError as e:
                        error = f"Enlace {rel}: {e.strerror or e}"
                with lock:
                    state["done"] += 1
                    count = state["done"]
                    if error:
                        report.errors.append(error)
                    if vanished:
                        report.vanished.append(label)
                    if changed:
                        report.changed.append(label)
                    report.bytes_written += written
                    if change:
                        changes.append(change)
                        if change.kind is ChangeKind.NEW and isinstance(action, Copy):
                            report.copied += 1
                        elif change.kind is ChangeKind.UPDATED:
                            report.updated += 1
                # El disco puede irse en cualquier momento. Mirarlo cada 50 evita cientos de
                # errores idénticos y seguir escribiendo contra una carpeta que ya no está.
                if count % 50 == 0 and not root.exists():
                    with lock:
                        state["disconnected"] = True
                    return
                self._progress(count, total, rel)

        threads = [threading.Thread(target=worker, daemon=True)
                   for _ in range(min(CONCURRENT_COPIES, len(actions)))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Ordenadas: en el registro se leen mejor como se ve el árbol.
        report.note_all(sorted(changes, key=lambda c: c.path))
        if state["disconnected"]:
            raise Disconnected()
        return int(state["done"])

    def _copy_atomically(self, src: Path, dst: Path, size: int = 0,
                         mtime_ns: int | None = None) -> bool:
        """A un temporal al lado, y sólo entonces a su sitio.

        Si el disco se desconecta a mitad, lo que queda es un `.bssd-parcial` evidente, y no
        un archivo con el nombre bueno y la mitad del contenido, que es lo que hace daño
        porque parece correcto.

        Devuelve si el archivo cambió mientras se copiaba: lo que ha quedado en el disco no
        es de fiar y se ha marcado para rehacerlo.
        """
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(f".{dst.name}{PARTIAL_SUFFIX}")
        tmp.unlink(missing_ok=True)
        try:
            if size >= STREAMING_THRESHOLD:
                self._copy_in_chunks(src, tmp)
            else:
                # copy_file_range en Linux: sin pasar los datos por Python.
                shutil.copyfile(src, tmp, follow_symlinks=False)
        except BaseException:
            tmp.unlink(missing_ok=True)       # no dejar el trozo a medias
            raise

        # La fecha **que tenía cuando se miró**, no la de ahora: si alguien lo estaba
        # guardando, lo que ha ido al disco es medio viejo y medio nuevo; con la fecha
        # nueva, la próxima pasada lo daría por bueno para siempre.
        stamp = mtime_ns if mtime_ns is not None else os.stat(src).st_mtime_ns
        try:
            os.utime(tmp, ns=(stamp, stamp))
        except OSError:
            pass
        try:
            # Los permisos, si el disco los guarda (exFAT no; ahí no pasa nada).
            os.chmod(tmp, stat.S_IMODE(os.stat(src).st_mode) | stat.S_IWUSR)
        except OSError:
            pass
        # os.replace sustituye aunque el de destino sea de sólo lectura (los objetos de git
        # lo son): en Linux eso lo decide la carpeta, no el archivo. El candado `uchg` del
        # Mac, que sí lo impedía, aquí no existe.
        os.replace(tmp, dst)

        if mtime_ns is None or not self._changed_while_copying(src, size, mtime_ns):
            return False
        # Medio de antes y medio de después: una fecha que no pueda confundirse con la
        # buena. Dejar la vieja no bastaría: si el guardado fue medio segundo después, la
        # tolerancia de exFAT los vería iguales. Restando algo más, se recopia seguro.
        evident = mtime_ns - (MTIME_TOLERANCE_NS + 1_000_000_000)
        try:
            os.utime(dst, ns=(evident, evident))
        except OSError:
            pass
        return True

    @staticmethod
    def _changed_while_copying(path: Path, size: int, mtime_ns: int) -> bool:
        """Lo mismo que hace rsync: tamaño y fecha antes y después, sin tolerancia (es el
        mismo archivo, en el mismo disco, mirado dos veces seguidas)."""
        try:
            now = os.stat(path, follow_symlinks=False)
        except OSError:
            return False
        return now.st_size != size or now.st_mtime_ns != mtime_ns

    def _copy_in_chunks(self, src: Path, dst: Path) -> None:
        with open(src, "rb") as reader, open(dst, "wb") as writer:
            while True:
                if self.cancelled:
                    raise Stopped()
                chunk = reader.read(CHUNK_SIZE)
                if not chunk:
                    break
                writer.write(chunk)

    @staticmethod
    def _copy_symlink(src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        target = os.readlink(src)
        # lexists y no exists: un enlace roto «no existe» y crearlo encima fallaría.
        if os.path.lexists(dst):
            _remove(dst)
        os.symlink(target, dst)

    # --- Mover -----------------------------------------------------------------------

    def _move_in_parallel(self, actions: list[Move], source: Path, dest: Path,
                          folder_name: str, root: Path, done: int, total: int,
                          report: SyncReport) -> int:
        """La única parte que borra algo del ordenador, así que el orden no se negocia: se
        copia, se relee del disco y se compara con el original, y **sólo entonces** se
        borra. Las carpetas no se borran nunca: el esqueleto se queda, vacío."""
        if not actions:
            return done
        lock = threading.Lock()
        # Nombres del disco que algún hilo ya ha pedido para sí, escritos o no. Dos hilos
        # que eligieran el mismo «-2» harían que el segundo pisara al primero, y del primero
        # ya no queda copia en el ordenador.
        reserved: set[str] = set()
        state = {"next": 0, "done": done, "disconnected": False}
        changes: list[Change] = []

        def exists(path: Path) -> bool:
            return os.path.lexists(path)       # lstat: un enlace roto también ocupa

        def free_name(preferred: Path) -> Path:
            base, ext = os.path.splitext(preferred.name)
            n = 2
            while True:
                candidate = preferred.with_name(f"{base}-{n}{ext}")
                if str(candidate) not in reserved and not exists(candidate):
                    reserved.add(str(candidate))
                    return candidate
                n += 1

        def reserve(preferred: Path) -> tuple[Path, bool]:
            with lock:
                if str(preferred) not in reserved:
                    reserved.add(str(preferred))
                    return preferred, exists(preferred)
                return free_name(preferred), False

        def worker() -> None:
            while True:
                with lock:
                    i = state["next"]
                    state["next"] = i + 1
                    if i >= len(actions) or state["disconnected"] or self.cancelled:
                        return
                action = actions[i]
                label = f"{folder_name}/{action.rel}"
                outcome = self._move_one(source / action.rel, dest / action.rel, action,
                                         label, reserve, lambda p: free_name_locked(p))
                with lock:
                    state["done"] += 1
                    count = state["done"]
                    kind, value, written, renamed_to = outcome
                    if kind == "moved":
                        report.moved += 1
                        report.bytes_written += written
                        changes.append(Change(ChangeKind.MOVED, label, written))
                        if renamed_to:
                            report.renamed.append(f"{label} → {renamed_to}")
                    elif kind == "error":
                        report.errors.append(value)
                    elif kind == "vanished":
                        report.vanished.append(label)
                    elif kind == "unverified":
                        report.unverified.append(label)
                if count % 50 == 0 and not root.exists():
                    with lock:
                        state["disconnected"] = True
                    return
                self._progress(count, total, label)

        def free_name_locked(preferred: Path) -> Path:
            with lock:
                return free_name(preferred)

        threads = [threading.Thread(target=worker, daemon=True)
                   for _ in range(min(CONCURRENT_COPIES, len(actions)))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        report.note_all(sorted(changes, key=lambda c: c.path))
        if state["disconnected"]:
            raise Disconnected()
        return int(state["done"])

    def _move_one(self, src: Path, preferred: Path, action: Move, label: str,
                  reserve: Callable[[Path], tuple[Path, bool]],
                  free_name: Callable[[Path], Path]) -> tuple[str, str, int, str | None]:
        """Copiar, comprobar, borrar. Devuelve (qué pasó, mensaje, bytes, renombrado a)."""
        # 1. ¿Con qué nombre entra?
        target, occupied = reserve(preferred)
        renamed_to = None if target == preferred else target.name

        # 2. Si ya había algo ahí, ¿es lo mismo?
        if occupied:
            if self._same(src, target, action.size, action.is_symlink):
                # Ya estaba archivado e idéntico: duplicarlo como «-2» sólo llenaría el disco
                # cada vez que el archivo reapareciera. Basta con quitarlo de aquí.
                try:
                    _remove(src)
                    return "moved", "", 0, None
                except OSError as e:
                    if not os.path.lexists(src):
                        return "vanished", "", 0, None
                    return "error", f"{label}: {e.strerror or e}", 0, None
            # Otro archivo con el mismo nombre: el de antes no se toca —ya no está en el
            # ordenador y no hay otra copia— y el que llega entra a su lado.
            target = free_name(preferred)
            renamed_to = target.name

        # 3. Copiar, comprobar y sólo entonces borrar.
        try:
            written = 0
            if action.is_symlink:
                self._copy_symlink(src, target)
            else:
                self._copy_atomically(src, target, action.size, action.mtime_ns)
                written = action.size
            if self._same(src, target, action.size, action.is_symlink):
                if not action.is_symlink:
                    # La fecha buena: acabamos de comparar el contenido entero.
                    try:
                        os.utime(target, ns=(action.mtime_ns, action.mtime_ns))
                    except OSError:
                        pass
                _remove(src)
                return "moved", "", written, renamed_to
            # La comparación se corta al cancelar y dice que no cuadran: se deshace igual,
            # pero no es sospechoso, sólo se ha pedido parar.
            _remove_quietly(target)
            if self.cancelled:
                return "stopped", "", 0, None
            # Lo que llegó no es lo que salió. El original se queda para volver a intentarlo.
            return "unverified", "", 0, None
        except Stopped:
            return "stopped", "", 0, None
        except OSError as e:
            if self.cancelled:
                return "stopped", "", 0, None
            if not os.path.lexists(src):
                return "vanished", "", 0, None
            return "error", f"{label}: {e.strerror or e}", 0, None

    def _same(self, here: Path, disk: Path, size: int, is_symlink: bool) -> bool:
        """Si el del disco es el mismo que el del ordenador, **byte a byte**: lo que viene
        después es borrar el original, y si la comprobación se equivoca no hay otra copia."""
        if is_symlink:
            try:
                return os.readlink(here) == os.readlink(disk)
            except OSError:
                return False
        try:
            a, b = os.lstat(here), os.lstat(disk)
        except OSError:
            return False
        if not (stat.S_ISREG(a.st_mode) and stat.S_ISREG(b.st_mode)):
            return False
        if a.st_size != b.st_size or a.st_size != size:
            return False
        return self._same_contents(here, disk)

    def _same_contents(self, a: Path, b: Path) -> bool:
        """Por trozos: un vídeo de 20 GB leído entero se llevaría por delante la memoria."""
        try:
            with open(a, "rb") as fa, open(b, "rb") as fb:
                while True:
                    if self.cancelled:
                        return False
                    ca, cb = fa.read(CHUNK_SIZE), fb.read(CHUNK_SIZE)
                    if ca != cb:
                        return False
                    if not ca:
                        return True
        except OSError:
            return False

    # --- Recorrer --------------------------------------------------------------------

    def _scan(self, base: Path, matcher: ExcludeMatcher, skip: set[str],
              is_destination: bool = False) -> ScanResult:
        """Lista recursiva indexada por ruta relativa.

        No sigue enlaces simbólicos —los anota como tales—: seguirlos es la forma clásica de
        copiar el disco entero sin querer, o de entrar en un bucle infinito.

        `skip`: rutas relativas de otras carpetas configuradas. Se compara la ruta entera, no
        el nombre: saltarse «otros» por el nombre se llevaría todos los «otros» del árbol.
        """
        entries: dict[str, Entry] = {}
        errors: list[str] = []
        stack: list[tuple[Path, str]] = [(base, "")]
        while stack:
            if self.cancelled:
                break
            directory, prefix = stack.pop()
            try:
                with os.scandir(directory) as it:
                    children = list(it)
            except OSError as error:
                errors.append(f"No se pudo leer {directory.name or directory}: "
                              f"{error.strerror or error}")
                continue
            for child in children:
                name = child.name
                if matcher.matches(name):
                    continue
                rel = f"{prefix}{name}"
                if rel in skip:
                    continue
                if is_destination and name.endswith(PARTIAL_SUFFIX):
                    # Un trozo de una copia que se cortó (disco desenchufado, apagón). No es
                    # de nadie: se borra en vez de retirarlo como si fuera un archivo.
                    _remove_quietly(Path(child.path))
                    continue
                try:
                    st = child.stat(follow_symlinks=False)
                except OSError as error:
                    errors.append(f"No se pudo leer {rel}: {error.strerror or error}")
                    continue
                is_link = stat.S_ISLNK(st.st_mode)
                is_dir = stat.S_ISDIR(st.st_mode)
                target = None
                if is_link:
                    try:
                        target = os.readlink(child.path)
                    except OSError:
                        target = None
                elif not (is_dir or stat.S_ISREG(st.st_mode)):
                    continue        # tuberías, sockets, dispositivos: no se copian
                entries[rel] = Entry(rel=rel, is_dir=is_dir, is_symlink=is_link,
                                     size=0 if is_dir or is_link else st.st_size,
                                     mtime_ns=st.st_mtime_ns, link_target=target)
                if is_dir:
                    stack.append((Path(child.path), rel + "/"))
        return ScanResult(entries, errors)

    # --- Decidir ---------------------------------------------------------------------

    def _admissible(self, source: dict[str, Entry], restricted: bool, insensitive: bool,
                    folder_name: str, report: SyncReport) -> dict[str, Entry]:
        """Lo que de verdad se puede llevar al disco. Lo demás se salta, con el motivo.

        Dos cosas que en el Mac no pasaban:
        - nombres que exFAT no admite (`:`, `?`, `"`… o un punto al final), que en ext4 son
          normales;
        - dos archivos que sólo se distinguen por las mayúsculas (`Foto.jpg` y `foto.jpg`),
          que en ext4 pueden convivir y en el exFAT del disco serían el mismo: se copiaría
          uno encima del otro en cada pasada. Se queda el primero en orden y el resto se
          salta. Con una carpeta, todo lo de dentro va con ella.
        """
        kept: dict[str, Entry] = {}
        dropped_prefixes: list[str] = []
        by_key: dict[str, str] = {}
        for rel in sorted(source):
            if any(rel.startswith(p) for p in dropped_prefixes):
                continue
            entry = source[rel]
            name = rel.rsplit("/", 1)[-1]
            reason = names.invalid_reason(name) if restricted else None
            if reason is None and insensitive:
                key = names.collision_key(rel)
                other = by_key.get(key)
                if other is not None:
                    reason = (f"en el disco sería el mismo que «{other.rsplit('/', 1)[-1]}», "
                              "porque no distingue mayúsculas")
                else:
                    by_key[key] = rel
            if reason is not None:
                report.skipped.append(f"{folder_name}/{rel}: {reason}")
                if entry.is_dir and not entry.is_symlink:
                    dropped_prefixes.append(rel + "/")
                continue
            kept[rel] = entry
        return kept

    def _make_plan(self, source: dict[str, Entry], destination: dict[str, Entry],
                   insensitive: bool, report: SyncReport) -> list[Action]:
        destination, renames = self._square_names(source, destination, insensitive)

        dirs: list[tuple[int, str]] = []
        copies: list[tuple[str, Action]] = []
        for rel, e in source.items():
            if e.is_dir and not e.is_symlink:
                # Las carpetas primero y de menos honda a más: el padre antes que el hijo.
                if rel not in destination:
                    dirs.append((depth(rel), rel))
                continue
            d = destination.get(rel)
            if e.is_symlink:
                # Un enlace que ya está y apunta a lo mismo no se toca.
                if d is None or not d.is_symlink or d.link_target != e.link_target:
                    copies.append((rel, Link(rel)))
                else:
                    report.unchanged += 1
                continue
            if d is None or d.is_dir or d.is_symlink:
                copies.append((rel, Copy(rel, e.size, e.mtime_ns, True)))
            elif e.size != d.size or abs(e.mtime_ns - d.mtime_ns) > MTIME_TOLERANCE_NS:
                copies.append((rel, Copy(rel, e.size, e.mtime_ns, False)))
            else:
                report.unchanged += 1

        # Lo que sobra en el disco, de más hondo a menos: vaciar la carpeta antes de
        # retirarla.
        leftovers = sorted(((depth(rel), Retire(rel, d.is_dir))
                            for rel, d in destination.items() if rel not in source),
                           key=lambda t: -t[0])
        dirs.sort()
        copies.sort(key=lambda t: t[0])
        return [*renames, *(MakeDir(rel) for _, rel in dirs), *(a for _, a in copies),
                *(a for _, a in leftovers)]

    def _square_names(self, source: dict[str, Entry], destination: dict[str, Entry],
                      insensitive: bool) -> tuple[dict[str, Entry], list[Action]]:
        """Lo que en el disco se llama como en el ordenador salvo por las mayúsculas (si el
        disco no las distingue) o por la forma Unicode de las tildes (siempre).

        Comparados como texto eran dos: uno nuevo que copiar y otro que ya no estaba. La
        copia caía encima del que había y al retirar el «viejo» se retiraba esa copia. Aquí
        se emparejan, se renombra en el disco y el resto del plan se hace como si ya se
        llamaran igual. Sólo se renombra lo que cambia en su propio nombre: si cambia una
        carpeta, lo de dentro la sigue.
        """
        from_source: dict[str, str] = {}
        doubtful: set[str] = set()
        for rel in source:
            key = names.pairing_key(rel, insensitive)
            if key in from_source:
                doubtful.add(key)          # no se adivina cuál es cuál
            else:
                from_source[key] = rel

        squared = dict(destination)
        renames: list[tuple[int, Rename]] = []
        for rel, d in destination.items():
            if rel in source:
                continue
            key = names.pairing_key(rel, insensitive)
            theirs = from_source.get(key)
            if key in doubtful or theirs is None or theirs in destination:
                continue
            del squared[rel]
            squared[theirs] = Entry(theirs, d.is_dir, d.is_symlink, d.size, d.mtime_ns,
                                    d.link_target)
            disk_name = rel.rsplit("/", 1)[-1]
            if disk_name == theirs.rsplit("/", 1)[-1]:
                continue
            parent = theirs.rsplit("/", 1)[0] if "/" in theirs else ""
            old = f"{parent}/{disk_name}" if parent else disk_name
            renames.append((depth(theirs), Rename(old, theirs)))
        renames.sort(key=lambda t: t[0])
        return squared, [r for _, r in renames]

    @staticmethod
    def _make_move_plan(source: dict[str, Entry]) -> list[Action]:
        """Todo lo del ordenador se lleva al disco. No se compara ni se retira nada."""
        dirs = sorted((depth(rel), rel) for rel, e in source.items()
                      if e.is_dir and not e.is_symlink)
        files = sorted(rel for rel, e in source.items() if not (e.is_dir and not e.is_symlink))
        return [*(MakeDir(rel) for _, rel in dirs),
                *(Move(rel, source[rel].size, source[rel].mtime_ns, source[rel].is_symlink)
                  for rel in files)]

    # --- Retirar y purgar ------------------------------------------------------------

    def _retire(self, victim: Path, target: Path) -> None:
        """Aparta a `_Retirados/fecha/…` conservando la ruta."""
        target.parent.mkdir(parents=True, exist_ok=True)
        # Al retirar una carpeta, sus archivos ya se apartaron antes (de dentro hacia
        # fuera), así que en _Retirados ya hay una carpeta igual: se funden, no se crea una
        # «Fotos-2» que partiría la retirada en dos. Ojo con los enlaces: uno a una carpeta
        # parece carpeta, y fundirlo se llevaría los archivos a los que apunta.
        if victim.is_dir() and not victim.is_symlink() and target.is_dir() \
                and not target.is_symlink():
            for child in os.listdir(victim):
                try:
                    self._retire(victim / child, target / child)
                except OSError:
                    pass
            victim.rmdir()
            return
        # El mismo archivo retirado dos veces el mismo día no pisa al primero.
        final = target
        base, ext = os.path.splitext(target.name)
        n = 2
        while os.path.lexists(final):
            final = target.with_name(f"{base}-{n}{ext}")
            n += 1
        os.rename(victim, final)

    @staticmethod
    def _purge_old_retired(root: Path, days: int, report: SyncReport) -> int:
        base = root / RETIRED_DIR
        try:
            listing = os.listdir(base)
        except OSError:
            return 0
        limit = date.today() - timedelta(days=days)
        purged = 0
        for name in listing:
            try:
                when = datetime.strptime(name, "%Y-%m-%d").date()
            except ValueError:
                continue
            if when >= limit:
                continue
            try:
                shutil.rmtree(base / name)
                purged += 1
            except OSError as error:
                report.errors.append(f"No se pudo purgar {RETIRED_DIR}/{name}: "
                                     f"{error.strerror or error}")
        return purged

    # --- Comprobaciones --------------------------------------------------------------

    @staticmethod
    def _assert_not_nested(source: str, destination: Path) -> None:
        """Ni el destino dentro del origen (copia sin fin) ni el origen dentro del destino
        (el motor «retiraría» los originales por no encontrarlos donde no están)."""
        s = os.path.realpath(source)
        d = os.path.realpath(destination)
        if d == s or d.startswith(s + "/"):
            raise Nested(f"El destino {d} está dentro del origen {s}: sería una copia sin fin.")
        if s.startswith(d + "/"):
            raise Nested(f"El origen {s} está dentro del destino {d}.")

    @staticmethod
    def free_space(path: Path) -> int | None:
        """Lo que queda libre, o None si no se sabe (entonces se intenta la copia)."""
        probe = path
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            return shutil.disk_usage(probe).free
        except OSError:
            return None


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _remove_quietly(path: Path) -> None:
    try:
        _remove(path)
    except OSError:
        pass
