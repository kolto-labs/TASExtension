"""telos: command line, REPL and text views for InputInterfaceTelos."""


def main(argv=None) -> int:
    from kptas.telos.cli import main as _main
    return _main(argv)


__all__ = ["main"]
