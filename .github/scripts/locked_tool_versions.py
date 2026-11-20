"""Print the build tool versions pinned in the repository's Poetry lock file."""

import argparse
import tomllib
from pathlib import Path

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Print locked wheel build tool versions.")
    parser.add_argument("names", nargs="+")
    parser.add_argument("--github-output", action="store_true")
    arguments = parser.parse_args()

    with Path("poetry.lock").open("rb") as source:
        packages = tomllib.load(source)["package"]

    for name in arguments.names:
        version = next((package["version"] for package in packages if package["name"] == name), None)
        if version is None:
            parser.error(f"{name!r} is missing from poetry.lock")
        print(f"{name}={version}" if arguments.github_output else version)
