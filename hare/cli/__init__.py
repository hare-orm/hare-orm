"""Hare CLI entry points."""

__all__ = ["main"]


def main() -> None:
    from hare.cli.hare_cli import HareCLI

    HareCLI.main()
