"""Punto de entrada: con argumentos, consola; sin ellos, la bandeja (todavía no existe)."""

import sys


def main() -> int:
    args = sys.argv[1:]
    if args:
        from . import cli
        return cli.main(args)
    print("La bandeja de LinBackup todavía no existe; de momento, sólo el modo consola.\n"
          "Prueba: linbackup --help", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
