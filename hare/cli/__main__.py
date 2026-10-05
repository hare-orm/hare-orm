from __future__ import annotations

from hare.cli.hare_cli import HareCLI

# A child process the CLI starts (stubgen's module inspection) imports this module again.
if __name__ == "__main__":
    HareCLI.main()
