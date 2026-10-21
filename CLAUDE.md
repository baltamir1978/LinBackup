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

## Pendiente de preguntar a Bruno

- **¿El mismo SSD se usará desde el Mac y desde Linux?** Si es así, la estructura del disco
  (`_Retirados`, temporales y la configuración que se guarde en él) tiene que ser compatible con la
  de BackupSSD.
