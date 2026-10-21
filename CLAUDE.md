# LinBackup — contexto del repositorio

Versión para Linux de **BackupSSD** (macOS), escrita **desde cero en Python**, no portada. Lo común
a las apps de Linux (pila, `.deb`, dónde se prueba, la bandeja) está en el `CLAUDE.md` de la
carpeta de proyectos; aquí va lo propio de esta.

## Estado (21-oct-2026)

**Sin empezar.** Va después de LinVideo, que es la piloto.

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

## Lo nuevo en Linux, que el original no tenía que resolver

- **ext4 distingue mayúsculas y APFS no.** En Linux pueden estar `Foto.jpg` y `foto.jpg` en la misma
  carpeta, y en el exFAT del disco serían el mismo archivo. Hay que detectarlo al planificar, avisar
  en el registro y no pisar uno con otro. Necesita un test propio.
- **Caracteres que exFAT no admite** (`: * ? " < > | \`, y los nombres que acaban en espacio o en
  punto). En ext4 son nombres válidos y el driver de exFAT del kernel los rechaza. Decidir si se
  saltan con aviso o se traducen (y entonces la traducción debe ser reversible y estable, o cada
  pasada los verá como nuevos).
- **El driver de exFAT del kernel** también ignora mayúsculas, como el de macOS, así que lo de
  «cambiar sólo las mayúsculas» funciona igual.

## El mismo SSD para el Mac y para Linux (Bruno, 21-oct-2026)

Bruno quiere poder usar el mismo disco con BackupSSD en el Mac y con LinBackup en Linux. Lo que
eso exige:

- **Cada equipo escribe en su propia carpeta raíz del disco.** BackupSSD guarda todo bajo
  `rootFolder` (por omisión `Backup-SSD`). Si los dos equipos usaran la misma raíz, cada uno
  apartaría a `_Retirados` lo que hubiera puesto el otro, porque no lo tiene en su lado. LinBackup
  usa otra por omisión (p. ej. `Backup-Debian`) y **se niega a usar una raíz que tenga pinta de
  ser de otro equipo**. Para saberlo hace falta una marca en la raíz con el equipo que la creó, y
  BackupSSD hoy no la deja: añadirla también allí, o al menos detectar una raíz con contenido y sin
  marca, y preguntar.
- **La misma forma dentro de la raíz**: `<raíz>/<carpeta>/…`, `_Retirados/AAAA-MM-DD/` con la ruta
  conservada, temporales `.bssd-parcial`. Así el disco se lee igual desde cualquiera de los dos.
- **La configuración no va en el disco**: BackupSSD la guarda en `~/.config/backup-ssd/config.json`,
  un perfil por disco con su `volumeUUID`. Usar el mismo formato JSON y la misma ruta, para poder
  copiar el perfil de un equipo a otro.
- **Normalización Unicode de los nombres**: el Mac suele escribir los nombres descompuestos (NFD,
  «cancio´n») y Linux compuestos (NFC, «canción»). Para exFAT son nombres distintos. BackupSSD no lo
  trata (no hay rastro de NFC/NFD en su código). Comparar siempre en NFC, igual que se compara sin
  mayúsculas, o los nombres con tilde se retirarán y se volverán a copiar en cada pasada.
