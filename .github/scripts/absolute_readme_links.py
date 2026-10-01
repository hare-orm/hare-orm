"""Makes the relative links of README.md absolute, for the package's description on PyPI.

GitHub resolves ``docs/assets/readme-banner-light.png`` and ``LICENSE`` against the repository;
PyPI, showing the README the package carries, has nothing to resolve them against. Before a
release builds the package, this rewrites them to the commit being released: an image to
``https://raw.githubusercontent.com/<repository>/<commit>/<path>``, a link to
``https://github.com/<repository>/blob/<commit>/<path>``. Absolute URLs and ``#anchors`` stay.

Usage:
    python .github/scripts/absolute_readme_links.py --repository hare-orm/hare-orm --commit <sha>
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


class ReadmeLinks:
    """Rewrites the relative links of one README."""

    #: A Markdown image or link: ``![alt](target)`` / ``[text](target)``.
    MARKDOWN_TARGET = re.compile(r"(?P<image>!?)\[(?P<text>[^\]]*)\]\((?P<target>[^)\s]+)\)")
    #: An HTML attribute naming a file: ``src``, ``srcset`` or ``href``.
    HTML_TARGET = re.compile(r'(?P<attribute>\b(?:src|srcset|href))="(?P<target>[^"]+)"')

    def __init__(self, repository: str, commit: str) -> None:
        """
        Args:
            repository: The GitHub repository, ``owner/name``.
            commit: The commit the links point at.
        """
        self.raw_base = f"https://raw.githubusercontent.com/{repository}/{commit}/"
        self.blob_base = f"https://github.com/{repository}/blob/{commit}/"

    @staticmethod
    def is_relative(target: str) -> bool:
        """Whether a link target is a path in the repository."""
        return not re.match(r"^[a-z][a-z0-9+.-]*:|^#|^/", target, re.IGNORECASE)

    def get_absolute(self, target: str, image: bool) -> str:
        """The absolute URL of a relative target.

        Args:
            target: The relative target.
            image: Whether it is an image - served raw rather than as a GitHub page.

        Returns:
            The URL.
        """
        return (self.raw_base if image else self.blob_base) + target.removeprefix("./")

    def rewrite(self, text: str) -> str:
        """The README with its relative links made absolute.

        Args:
            text: The README.

        Returns:
            The rewritten README.
        """

        def replace_markdown(match: re.Match[str]) -> str:
            target = match.group("target")
            if not self.is_relative(target):
                return match.group(0)
            image = bool(match.group("image"))
            return f"{match.group('image')}[{match.group('text')}]({self.get_absolute(target, image)})"

        def replace_html(match: re.Match[str]) -> str:
            target = match.group("target")
            if not self.is_relative(target):
                return match.group(0)
            image = match.group("attribute") in ("src", "srcset")
            return f'{match.group("attribute")}="{self.get_absolute(target, image)}"'

        return self.HTML_TARGET.sub(replace_html, self.MARKDOWN_TARGET.sub(replace_markdown, text))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Makes the relative links of README.md absolute.")
    parser.add_argument("--repository", required=True, help="The GitHub repository, owner/name.")
    parser.add_argument("--commit", required=True, help="The commit the links point at.")
    parser.add_argument("--readme", type=Path, default=Path(__file__).resolve().parents[2] / "README.md")
    arguments = parser.parse_args()
    readme = arguments.readme.read_text(encoding="utf-8")
    arguments.readme.write_text(ReadmeLinks(arguments.repository, arguments.commit).rewrite(readme), encoding="utf-8")
