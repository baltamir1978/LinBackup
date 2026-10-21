# LinBackup

Copia tus carpetas a un SSD externo en cuanto lo enchufas. Reconoce el disco por su UUID, aparta
lo borrado en vez de eliminarlo y compara byte a byte antes de mover nada.

Versión para Linux de [BackupSSD](https://github.com/baltamir1978/BackupSSD), la app de macOS. No
es un port: se escribe desde cero en Python, con la app original como especificación.

**Estado (21-oct-2026): el motor y el modo consola funcionan.** Falta la bandeja, que se entere
sola de que has enchufado el disco y que lo expulse.

## Lo que hace

- **El disco se reconoce por su UUID, nunca por su nombre**, y se vuelve a comprobar justo antes
  de escribir.
- **Lo que borras no se borra del disco**: se aparta a `_Retirados/AAAA-MM-DD/` y se elimina
  pasados 90 días (configurable; con 0, nunca).
- **Carpetas en modo sincronizar** (el disco queda igual) **o mover** (se lleva al disco y se borra
  de aquí, comparando byte a byte antes de borrar).
- **Planifica antes de escribir**: sabe si cabe antes de empezar, y el ensayo cuenta lo que haría
  sin tocar nada.
- **Cada archivo va a un temporal** y se pone en su sitio al terminar; si el disco se desconecta,
  no queda medio archivo con el nombre bueno.
- **Se entera de los archivos que cambian mientras se copian** y los rehace la próxima vez.

### Lo propio de Linux

- **Un disco exFAT no distingue mayúsculas, y ext4 sí.** Si tienes `Foto.jpg` y `foto.jpg` en la
  misma carpeta, en el disco serían el mismo archivo: se copia uno y se avisa del otro.
- **exFAT no admite algunos nombres** (`:`, `?`, `"`, o acabados en punto): se saltan y se dice
  cuáles, en vez de fallar en cada pasada.
- **El mismo SSD vale para el Mac y para Linux**: cada equipo escribe en su propia carpeta raíz, y
  LinBackup se niega a tocar una que no sea suya (la `Backup-SSD` del Mac, por ejemplo).

## Instalar en Debian

```sh
Tools/build-deb.sh
sudo apt install ./build/linbackup_*_all.deb
```

## Usar

Mientras no haya bandeja, la configuración se escribe a mano en `~/.config/linbackup/config.json`:

```sh
linbackup --volumenes                 # el UUID de tu disco
linbackup --ejemplo > ~/.config/linbackup/config.json    # y se edita
linbackup --estado                    # qué copiará y si hay algún problema
linbackup --ensayo                    # lo que haría, sin escribir nada
linbackup --sincronizar --detalle     # hacerlo, archivo a archivo
linbackup --historial
```

## Desarrollo

```sh
python3 -m venv .venv && .venv/bin/pip install pytest mypy
.venv/bin/pytest          # 85 pruebas; 9 de ellas sobre imágenes exFAT (sólo en el Mac)
.venv/bin/mypy
```

## Licencia

MIT, como BackupSSD.
