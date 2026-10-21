# LinBackup — contexto del repositorio

Versión para Linux de **BackupSSD** (macOS), escrita **desde cero en Python**, no portada. Lo común
a las apps de Linux (pila, `.deb`, dónde se prueba, la bandeja) está en el `CLAUDE.md` de la
carpeta de proyectos; aquí va lo propio de esta.

## Estado (21-oct-2026)

**Hecho y probado en el Mac: el motor, la configuración, el historial, la marca de la raíz, la
lectura de discos, el modo consola y el `.deb`.** 85 pruebas:
- las 139 comprobaciones de BackupSSD traducidas caso a caso (`tests/test_engine.py`, menos las de
  Fotosync, Itusync e iCloud, que en Linux no existen), también las de parar a mitad de un archivo
  de 2 GB y la del archivo que cambia mientras se copia;
- **9 sobre discos de verdad** (`tests/test_exfat_images.py`): una imagen exFAT como el SSD y otra
  APFS que distingue mayúsculas como origen, igual que el ext4 de Linux. Se crean con `hdiutil` y
  se desmontan solas. Sólo en el Mac;
- las nuevas de Linux: nombres que exFAT no admite, `Foto.jpg` y `foto.jpg` a la vez, tildes en
  NFD, la marca de la raíz, permisos y archivos de sólo lectura.

Probado también de principio a fin por consola contra un exFAT montado: se niega a escribir en
`Backup-SSD` (la raíz del Mac), copia y mueve en la suya, y la segunda pasada no hace nada.

**Falta** (y no se puede hacer sin Debian):
- **enterarse al momento de que se enchufa el disco y expulsarlo**: udisks2 por D-Bus (tabla de
  abajo). Hoy los discos se leen con `findmnt`/`lsblk`, que basta para saber qué hay montado;
- **la bandeja** y el arranque con la sesión;
- **probar `volumes.py` en Linux**: la parte de `findmnt` está escrita pero sin probar; en el Mac
  se usa `diskutil`.

## Cómo se prueba

```sh
.venv/bin/pytest && .venv/bin/mypy       # el entorno virtual está en .gitignore
Tools/build-deb.sh --check
```

Para el modo consola sin tocar la configuración de verdad: `XDG_CONFIG_HOME=… XDG_STATE_HOME=…
.venv/bin/python -m linbackup …`. Un disco de prueba: `hdiutil create -size 100m -fs ExFAT
-volname PRUEBA -type UDIF disco.dmg && hdiutil attach -nobrowse disco.dmg`.

**Ojo con las pruebas de mayúsculas de `test_engine.py`**: corren en el APFS del Mac, que no
distingue mayúsculas a ningún lado, como pasaba en BackupSSD. Lo de Linux (origen que sí las
distingue) lo cubren las de las imágenes.

## La especificación es el original

`../BackupSSD` (`github.com/baltamir1978/BackupSSD`). **Su `README.md` es la especificación
completa**; leerlo entero antes de escribir el motor. Los tests del original
(`macapp/tests/main.swift`, unas 1.250 líneas) **se traducen a `pytest` caso a caso**, y el motor no
se da por bueno hasta que los pase todos. Es la única app que borra cosas: aquí no vale «parece que
funciona».

Los principios que no se negocian:

- **El disco se reconoce por su UUID, nunca por su nombre**, y se vuelve a comprobar justo antes de
  escribir.
- **Lo que se borra en el ordenador no se borra del disco**: se aparta a `_Retirados/AAAA-MM-DD/`
  conservando la ruta, y se borra de verdad pasados N días (90 por omisión; con 0, nunca).
- **Carpetas en modo sincronizar y en modo mover.** Una carpeta en modo mover puede estar dentro de
  otra que se sincroniza.
- **Antes de borrar algo movido, se relee del disco y se compara byte a byte.** Si no coincide, se
  deshace y el archivo se queda donde estaba.
- **Se planifica antes de tocar nada**: se calcula el espacio y se sabe si cabe antes de empezar.
- **Cada archivo se copia a un temporal (`.bssd-parcial`) y se renombra al terminar.**
- **«Detener y expulsar» corta incluso a mitad de un archivo** (se copia por trozos para eso).
- **Se detectan los archivos en uso**: fecha y tamaño antes y después; si cambian, se rehace.
- **Cambiar sólo las mayúsculas de un nombre no saca nada del backup.**
- **Las fechas se comparan con 2 s de tolerancia**, porque exFAT las guarda con esa resolución; sin
  ella se recopiaría el disco entero en cada pasada.

## Cómo se hace en Linux

| En el Mac | Aquí |
|---|---|
| `NSWorkspace.didMountNotification` / `willUnmount` | **udisks2 por D-Bus** con Gio: señales `InterfacesAdded` / `InterfacesRemoved` del ObjectManager y la propiedad `MountPoints` |
| `volumeUUIDString` | `IdUUID` del bloque (en exFAT es el número de serie del volumen) |
| `unmountAndEjectDevice` | `Filesystem.Unmount` y después `Drive.PowerOff` |
| Montaje automático de macOS | GNOME monta en `/media/<usuario>/<etiqueta>`; sin sesión gráfica habría que llamar a `Filesystem.Mount` |
| `MenuBarExtra` | Bandeja con StatusNotifierItem (ver el `CLAUDE.md` común) |
| `SMAppService` / LaunchAgent | `.desktop` en `/etc/xdg/autostart/` (necesita la sesión para la bandeja). Servicio de usuario de systemd sólo si el motor se separa de la interfaz |
| Notificaciones | `Gio.Notification` |

## Lo nuevo en Linux, y cómo se ha resuelto

- **Lo que decide es el disco de destino**, no el origen. `names.filesystem_of` mira el tipo
  (`/proc/self/mounts` en Linux, `mount` en el Mac) y `names.is_case_insensitive` comprueba con un
  archivo que ya esté, sin escribir; si no se sabe, se supone que no distingue mayúsculas (tratar
  como uno lo que serían dos sólo cuesta una copia; lo contrario, perder un archivo).
- **`Foto.jpg` y `foto.jpg` a la vez** (posible en ext4): se copia el primero en orden y el otro se
  **salta con aviso** («en el disco sería el mismo que…»). Con una carpeta, todo lo de dentro.
  Sólo si el disco no distingue mayúsculas.
- **Nombres que exFAT no admite** (`: * ? " < > | \`, control, o acabados en espacio o punto,
  que el driver de Linux quitaría): **se saltan con aviso**, no se traducen. Una traducción tendría
  que ser reversible y estable, y saltar es lo seguro. Sólo en discos tipo FAT/exFAT/NTFS.
- **Tildes en NFD** (como las escribe el Mac): al emparejar nombres se compara siempre en NFC, y si
  el disco tiene la otra forma se **renombra** en el disco en vez de retirar y recopiar
  (`engine._square_names`, que generaliza el arreglo de las mayúsculas de BackupSSD).
- **Los `.bssd-parcial` que dejó un corte** se borran al recorrer el disco, en vez de retirarlos
  como si fueran archivos (BackupSSD los retiraba).
- **No hace falta quitar el candado `uchg`**: en Linux no existe. Los objetos de git son de sólo
  lectura, y eso no impide sustituirlos ni retirarlos (lo decide la carpeta). Probado.
- **Los permisos se conservan** si el disco los guarda (exFAT no).

## El mismo SSD para el Mac y para Linux (Bruno, 21-oct-2026)

Bruno quiere poder usar el mismo disco con BackupSSD en el Mac y con LinBackup en Linux. Lo que
eso exige:

- **Cada equipo escribe en su propia carpeta raíz del disco.** BackupSSD guarda todo bajo
  `rootFolder` (por omisión `Backup-SSD`). Si los dos equipos usaran la misma raíz, cada uno
  apartaría a `_Retirados` lo que hubiera puesto el otro, porque no lo tiene en su lado. LinBackup
  usa `Backup-Linux` por omisión y **se niega a usar una raíz con cosas que no lleve su marca**
  (`.linbackup-raiz.json`, con el `machine-id` del equipo; `rootmark.py`). `--adoptar` la marca a
  mano si de verdad es la de este equipo. **BackupSSD no deja marca**: su `Backup-SSD` queda
  protegida porque LinBackup no la toca, pero BackupSSD no sabría negarse a escribir en
  `Backup-Linux` si alguien le pusiera esa raíz. Añadirle la marca está pendiente de que Bruno lo
  pida.
- **La misma forma dentro de la raíz**: `<raíz>/<carpeta>/…`, `_Retirados/AAAA-MM-DD/` con la ruta
  conservada, temporales `.bssd-parcial`. Así el disco se lee igual desde cualquiera de los dos.
- **La configuración no va en el disco**: LinBackup la guarda en `~/.config/linbackup/config.json`
  con el mismo formato que BackupSSD (`~/.config/backup-ssd/config.json`). **Pero el perfil de un
  disco no se puede copiar tal cual de un equipo a otro: macOS y Linux no dan el mismo UUID para un
  exFAT.** Linux da el número de serie (`ABCD-1234`); macOS, uno largo inventado a partir de él
  (en las pruebas: `ACF90333-0F4F-3B3A-85C7-EE26BA0AC61B`).
- **Normalización Unicode de los nombres**: el Mac suele escribir los nombres descompuestos (NFD,
  «cancio´n») y Linux compuestos (NFC, «canción»). Para exFAT son nombres distintos. BackupSSD no lo
  trata (no hay rastro de NFC/NFD en su código). Resuelto aquí (ver arriba).
