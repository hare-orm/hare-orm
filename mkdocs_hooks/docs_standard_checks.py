"""The docs standard of CONTRIBUTING.md's Docs section, checked on every build of the site."""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Any

from hare.dialects.clickhouse.server_versions import CLICKHOUSE_MINIMUM_SERVER_VERSION
from hare.dialects.postgresql.server_versions import POSTGRESQL_MINIMUM_SERVER_VERSION
from hare.dialects.sqlite.server_versions import SQLITE_MINIMUM_SERVER_VERSION
from mkdocs_hooks.constants import (
    CHECKED_SITE_LANGUAGE,
    CODE_COMMENT,
    DATABASE_HEADING_FORM,
    DATABASE_NAMES_BY_FOLDER,
    EMPTY_LINK_TEXT,
    ENGLISH_REPLACED_WORDING,
    GENERATED_PAGE_PREFIXES,
    HOME_PAGE_NAME,
    HYPHEN_AS_DASH,
    INLINE_CODE,
    LINK_TARGET,
    MAX_PROSE_LINE_LENGTH,
    MAX_TABLE_CELL_LENGTH,
    ONE_LINE_DOCSTRING,
    OTHER_ORM_COMPARISONS,
    OTHER_ORM_PAGE_NAME,
    OTHER_ORM_PAGE_PREFIXES,
    RUSSIAN_REPLACED_WORDING,
    STRAIGHT_QUOTED_RUSSIAN,
    VERSION_REQUIREMENT,
)
from mkdocs_hooks.declarations import DocsProblem
from mkdocs_hooks.docs_page import DocsPage


class DocsStandardChecks:
    """Checks every page of ``docs/`` against the docs standard and reports each breach as a
    warning - which fails ``mkdocs build --strict``."""

    log = logging.getLogger("mkdocs.hooks.docs_standard_checks")
    #: The oldest server hare runs on, by database - a version a feature needs is named only above it.
    minimum_server_versions: dict[str, tuple[int, ...]] = {
        "PostgreSQL": POSTGRESQL_MINIMUM_SERVER_VERSION,
        "SQLite": SQLITE_MINIMUM_SERVER_VERSION,
        "ClickHouse": CLICKHOUSE_MINIMUM_SERVER_VERSION,
    }

    @staticmethod
    def on_pre_build(config: Any, **kwargs: Any) -> None:
        """Reports the breaches of the standard before the site is built - once, in the build of
        ``CHECKED_SITE_LANGUAGE``.

        Args:
            config: The site's configuration.
            kwargs: The rest of the event's arguments.
        """
        if config["theme"]["language"] != CHECKED_SITE_LANGUAGE:
            return
        for problem in DocsStandardChecks.get_problems(Path(config["docs_dir"])):
            DocsStandardChecks.log.warning("%s:%d: %s", problem.page, problem.line, problem.message)

    @staticmethod
    def get_problems(docs_directory: Path) -> list[DocsProblem]:
        """Every breach of the standard on the pages of ``docs_directory``.

        Args:
            docs_directory: The ``docs_dir`` of the site.

        Returns:
            The breaches, page by page.
        """
        pages = {
            page.name: page
            for page in map(lambda path: DocsPage(docs_directory, path), sorted(docs_directory.rglob("*.md")))
        }
        problems: list[DocsProblem] = []
        for page in pages.values():
            problems += DocsStandardChecks.check_headings(page)
            problems += DocsStandardChecks.check_code_blocks(page)
            problems += DocsStandardChecks.check_links(page, pages)
            if not page.name.startswith(GENERATED_PAGE_PREFIXES):
                problems += DocsStandardChecks.check_prose(page)
                problems += DocsStandardChecks.check_tables(page)
            sibling = pages.get(page.sibling_name)
            if sibling is None:
                problems.append(DocsProblem(page.name, 1, f"no {page.sibling_name} beside it"))
            elif not page.is_russian:
                problems += DocsStandardChecks.check_translation(page, sibling)
        problems += DocsStandardChecks.check_inbound_links(pages)
        return problems

    @staticmethod
    def check_headings(page: DocsPage) -> list[DocsProblem]:
        """The headings: an anchor on each section, no level below ``###``, a section about one
        database named ``Topic (Database)``, the title of a database's page ``Database: topic``,
        and text in every section.

        Args:
            page: The page.

        Returns:
            The breaches.
        """
        problems: list[DocsProblem] = []
        for index, heading in enumerate(page.headings):
            if not heading.title.strip():
                problems.append(DocsProblem(page.name, heading.line, "a heading without a title"))
            if heading.level > 3:
                problems.append(DocsProblem(page.name, heading.line, "a heading deeper than ###"))
            if heading.level in {2, 3} and heading.anchor is None:
                problems.append(DocsProblem(page.name, heading.line, 'a section without <a id="..."></a>'))
            if heading.level > 1 and DATABASE_HEADING_FORM.search(heading.title):
                problems.append(
                    DocsProblem(page.name, heading.line, "a section about one database is titled 'Topic (Database)'")
                )
            following = page.headings[index + 1] if index + 1 < len(page.headings) else None
            opens_subsection = following is not None and following.level > heading.level
            section_end = following.line if following is not None else len(page.lines) + 1
            has_code = any(heading.line < block.line < section_end for block in page.code_blocks)
            # A section going straight into its subsections needs no text of its own - one holding code does.
            if heading.level > 1 and not page.heading_has_text[heading.line] and (has_code or not opens_subsection):
                problems.append(DocsProblem(page.name, heading.line, "a section of code without a sentence about it"))
        parts = page.english_name.split("/")
        if len(parts) == 3 and parts[0] == "dialects" and parts[1] in DATABASE_NAMES_BY_FOLDER and page.headings:
            title = page.headings[0]
            if not title.title.startswith(f"{DATABASE_NAMES_BY_FOLDER[parts[1]]}: "):
                problems.append(
                    DocsProblem(page.name, title.line, f"the title is '{DATABASE_NAMES_BY_FOLDER[parts[1]]}: topic'")
                )
        return problems

    @staticmethod
    def check_code_blocks(page: DocsPage) -> list[DocsProblem]:
        """The code blocks: a language on every fence, and Python that parses.

        Args:
            page: The page.

        Returns:
            The breaches.
        """
        problems = [
            DocsProblem(page.name, line, "a code block without a language") for line in page.unnamed_fence_lines
        ]
        for block in page.code_blocks:
            if block.language != "python":
                continue
            try:
                compile(block.code, page.name, "exec", flags=ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
            except SyntaxError as error:
                problems.append(DocsProblem(page.name, block.line, f"a python block that doesn't parse: {error.msg}"))
        return problems

    @staticmethod
    def check_links(page: DocsPage, pages: dict[str, DocsPage]) -> list[DocsProblem]:
        """The links: every link has a text, a Russian page links Russian pages.

        Args:
            page: The page.
            pages: Every page, by name.

        Returns:
            The breaches.
        """
        problems: list[DocsProblem] = [
            DocsProblem(page.name, line, "a link without a text")
            for line, text in [
                *((prose.line, prose.text) for prose in page.prose),
                *((line, " ".join(cells)) for table in page.tables for line, cells in table),
            ]
            if EMPTY_LINK_TEXT.search(text)
        ]
        if not page.is_russian:
            return problems
        for line, linked_name in page.get_linked_page_names():
            linked = pages.get(linked_name)
            if linked is not None and not linked.is_russian:
                problems.append(DocsProblem(page.name, line, f"a Russian page links the English {linked_name}"))
        return problems

    @staticmethod
    def check_prose(page: DocsPage) -> list[DocsProblem]:
        """The text: its length, its dashes and quotes, the words the standard replaces, comparisons
        with other ORMs and versions no newer than the oldest supported server.

        Args:
            page: The page.

        Returns:
            The breaches.
        """
        problems: list[DocsProblem] = []
        replaced_wording = RUSSIAN_REPLACED_WORDING if page.is_russian else ENGLISH_REPLACED_WORDING
        compares_orms = page.english_name.startswith(OTHER_ORM_PAGE_PREFIXES)
        for prose in page.prose:
            if len(LINK_TARGET.sub("]()", prose.text)) > MAX_PROSE_LINE_LENGTH:
                problems.append(DocsProblem(page.name, prose.line, f"a line longer than {MAX_PROSE_LINE_LENGTH}"))
            text = INLINE_CODE.sub("`code`", LINK_TARGET.sub("]()", prose.text))
            if HYPHEN_AS_DASH.search(text):
                problems.append(DocsProblem(page.name, prose.line, 'a dash written " - " - write "—"'))
            if page.is_russian and STRAIGHT_QUOTED_RUSSIAN.search(text):
                problems.append(DocsProblem(page.name, prose.line, "straight quotes - write «ёлочки»"))
            for pattern, advice in replaced_wording.items():
                if pattern.search(text):
                    problems.append(DocsProblem(page.name, prose.line, advice))
            # A link to the page comparing hare with Django names it.
            if not compares_orms and OTHER_ORM_COMPARISONS.search(text) and OTHER_ORM_PAGE_NAME not in prose.text:
                problems.append(
                    DocsProblem(page.name, prose.line, "a comparison with another ORM - describe hare's own rule")
                )
            problems += DocsStandardChecks.check_version_requirements(page, prose.line, text)
        return problems

    @staticmethod
    def check_version_requirements(page: DocsPage, line: int, text: str) -> list[DocsProblem]:
        """The versions a line names as the oldest one a feature needs - each newer than the oldest
        server hare runs on.

        Args:
            page: The page.
            line: The line's number.
            text: The line's text.

        Returns:
            The breaches.
        """
        problems: list[DocsProblem] = []
        for match in VERSION_REQUIREMENT.finditer(text):
            version = tuple(int(part) for part in match["version"].split("."))
            minimum = DocsStandardChecks.minimum_server_versions[match["database"]]
            if version <= minimum:
                problems.append(
                    DocsProblem(
                        page.name,
                        line,
                        f"{match[0]} - hare runs on {match['database']} {'.'.join(map(str, minimum))} and newer only",
                    )
                )
        return problems

    @staticmethod
    def check_tables(page: DocsPage) -> list[DocsProblem]:
        """The tables: every row has the cells of its header, none longer than ``MAX_TABLE_CELL_LENGTH``.

        Args:
            page: The page.

        Returns:
            The breaches.
        """
        problems: list[DocsProblem] = []
        for table in page.tables:
            header_cell_count = len(table[0][1])
            for line, cells in table:
                if len(cells) != header_cell_count:
                    problems.append(
                        DocsProblem(
                            page.name,
                            line,
                            f"a table row of {len(cells)} cells under a header of {header_cell_count} - "
                            "escape a pipe inside a cell as \\|",
                        )
                    )
                if any(len(cell) > MAX_TABLE_CELL_LENGTH for cell in cells):
                    problems.append(
                        DocsProblem(
                            page.name,
                            line,
                            f"a table cell longer than {MAX_TABLE_CELL_LENGTH} - move it into a section",
                        )
                    )
        return problems

    @staticmethod
    def check_translation(english: DocsPage, russian: DocsPage) -> list[DocsProblem]:
        """A Russian page against its English one: the same anchors in the same order, and the same
        code but for comments.

        Args:
            english: The English page.
            russian: Its Russian page.

        Returns:
            The breaches.
        """
        problems: list[DocsProblem] = []
        english_anchors = [heading.anchor for heading in english.headings]
        russian_anchors = [heading.anchor for heading in russian.headings]
        if english_anchors != russian_anchors:
            problems.append(DocsProblem(russian.name, 1, f"the headings differ from {english.name}'s"))
        english_code = [DocsStandardChecks.get_code_without_comments(block.code) for block in english.code_blocks]
        russian_code = [DocsStandardChecks.get_code_without_comments(block.code) for block in russian.code_blocks]
        if len(english_code) != len(russian_code):
            problems.append(
                DocsProblem(
                    russian.name, 1, f"{len(russian_code)} code blocks, {english.name} has {len(english_code)}"
                )
            )
            return problems
        for english_block, russian_block, code, russian_text in zip(
            english.code_blocks, russian.code_blocks, english_code, russian_code, strict=True
        ):
            # A text block is a figure - a tree, an output - its words translated with the page.
            if english_block.language != "text" and code != russian_text:
                problems.append(
                    DocsProblem(
                        russian.name, russian_block.line, f"the code differs from {english.name}:{english_block.line}"
                    )
                )
        return problems

    @staticmethod
    def get_code_without_comments(code: str) -> str:
        """The code with its comments, one-line docstrings and trailing spaces left out - what a
        translation keeps.

        Args:
            code: The code of a block.

        Returns:
            The code to compare.
        """
        lines = (CODE_COMMENT.sub("", line).rstrip() for line in code.splitlines())
        return "\n".join(line for line in lines if line and not ONE_LINE_DOCSTRING.match(line))

    @staticmethod
    def check_inbound_links(pages: dict[str, DocsPage]) -> list[DocsProblem]:
        """Every page but the home page is linked from another page, not only from the navigation.

        Args:
            pages: Every page, by name.

        Returns:
            The breaches.
        """
        linked: set[str] = set()
        for page in pages.values():
            linked.update(name for _line, name in page.get_linked_page_names() if name != page.name)
        return [
            DocsProblem(name, 1, "no other page links it")
            for name in pages
            if name not in linked and name not in {HOME_PAGE_NAME, HOME_PAGE_NAME.replace(".md", ".ru.md")}
        ]


# The name mkdocs calls the hook by.
on_pre_build = DocsStandardChecks.on_pre_build
