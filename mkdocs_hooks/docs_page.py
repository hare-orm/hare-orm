"""One Markdown page of the docs, split into what the docs standard is checked against."""

from __future__ import annotations

import posixpath
import textwrap
from pathlib import Path

from mkdocs_hooks.constants import (
    CODE_FENCE,
    HEADING_ANCHOR,
    MARKDOWN_HEADING,
    MARKDOWN_LINK,
    RUSSIAN_PAGE_SUFFIX,
    TABLE_CELL_SEPARATOR,
)
from mkdocs_hooks.declarations import PageCodeBlock, PageHeading, PageProse


class DocsPage:
    """A page of ``docs/``: its headings, code blocks, prose lines, table rows and the pages it
    links - each part with the number of the line it starts on."""

    def __init__(self, docs_directory: Path, path: Path) -> None:
        """Reads and splits the page.

        Args:
            docs_directory: The ``docs_dir`` of the site.
            path: The page's file.
        """
        #: The page's path under ``docs/``, with ``/`` separators - ``models/relations.md``.
        self.name = path.relative_to(docs_directory).as_posix()
        self.is_russian = self.name.endswith(RUSSIAN_PAGE_SUFFIX)
        self.lines = path.read_text(encoding="utf-8").splitlines()
        self.headings: list[PageHeading] = []
        self.code_blocks: list[PageCodeBlock] = []
        self.prose: list[PageProse] = []
        #: The tables of the page - each a list of its rows' cells, by the row's line, the header first.
        self.tables: list[list[tuple[int, list[str]]]] = []
        #: The line of each code block's opening fence that names no language.
        self.unnamed_fence_lines: list[int] = []
        #: Each heading's line -> whether text other than code follows it before the next heading.
        self.heading_has_text: dict[int, bool] = {}
        self._split()

    @property
    def sibling_name(self) -> str:
        """The name of the page in the other language - ``a.md`` for ``a.ru.md`` and back."""
        if self.is_russian:
            return self.name.removesuffix(RUSSIAN_PAGE_SUFFIX) + ".md"
        return self.name.removesuffix(".md") + RUSSIAN_PAGE_SUFFIX

    @property
    def english_name(self) -> str:
        """The name of the English page of the pair."""
        return self.sibling_name if self.is_russian else self.name

    def get_linked_page_names(self) -> list[tuple[int, str]]:
        """The pages of the site this page links, by the line of the link - external links and
        links within the page left out.

        Returns:
            (line, page name under ``docs/``) of each link.
        """
        linked: list[tuple[int, str]] = []
        directory = posixpath.dirname(self.name)
        for line_number, text in self._get_lines_outside_code():
            for match in MARKDOWN_LINK.finditer(text):
                target = match["target"].split("#", 1)[0]
                if not target.endswith(".md") or "://" in target:
                    continue
                linked.append((line_number, posixpath.normpath(posixpath.join(directory, target))))
        return linked

    def _get_lines_outside_code(self) -> list[tuple[int, str]]:
        """The lines not inside a fenced code block - prose, headings and tables.

        Returns:
            (line number, text) of each such line.
        """
        outside: list[tuple[int, str]] = []
        in_code = False
        for line_number, text in enumerate(self.lines, start=1):
            if CODE_FENCE.match(text):
                in_code = not in_code
                continue
            if not in_code:
                outside.append((line_number, text))
        return outside

    def _split(self) -> None:
        """Splits the lines into headings, code blocks, table rows and prose."""
        in_code = False
        fence_line = 0
        fence_indent = ""
        language = ""
        code_lines: list[str] = []
        current_heading_line: int | None = None
        previous_line_in_table = False
        for line_number, text in enumerate(self.lines, start=1):
            line_in_table = previous_line_in_table
            previous_line_in_table = False
            fence = CODE_FENCE.match(text)
            if fence and not in_code:
                in_code = True
                fence_line, fence_indent, language = line_number, fence["indent"], fence["language"]
                code_lines = []
                if not language:
                    self.unnamed_fence_lines.append(line_number)
                continue
            if fence and in_code:
                in_code = False
                # An empty line of a quote is its marker alone - ">" without the space after it.
                code = textwrap.dedent(
                    "\n".join(
                        "" if line.rstrip() == fence_indent.rstrip() else line.removeprefix(fence_indent)
                        for line in code_lines
                    )
                )
                self.code_blocks.append(PageCodeBlock(fence_line, language, code))
                continue
            if in_code:
                code_lines.append(text)
                continue
            heading = MARKDOWN_HEADING.match(text)
            if heading:
                title = heading["title"]
                anchor = HEADING_ANCHOR.match(title)
                self.headings.append(
                    PageHeading(
                        line_number,
                        len(heading["level"]),
                        anchor["anchor"] if anchor else None,
                        title[anchor.end() :] if anchor else title,
                    )
                )
                current_heading_line = line_number
                self.heading_has_text[line_number] = False
                continue
            stripped = text.strip()
            if not stripped:
                continue
            if current_heading_line is not None:
                self.heading_has_text[current_heading_line] = True
            if stripped.startswith("|"):
                if not line_in_table:
                    self.tables.append([])
                cells = [cell.strip() for cell in TABLE_CELL_SEPARATOR.split(stripped.strip("|"))]
                self.tables[-1].append((line_number, cells))
                previous_line_in_table = True
            elif not stripped.startswith("<"):
                self.prose.append(PageProse(line_number, text))
