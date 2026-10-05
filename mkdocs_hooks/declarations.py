"""The parts of a docs page the standard checks read, and a breach of the standard."""

from __future__ import annotations

from typing import NamedTuple


class PageHeading(NamedTuple):
    """A heading of a page, outside its code."""

    line: int
    level: int
    anchor: str | None
    title: str


class PageCodeBlock(NamedTuple):
    """A fenced code block of a page - its code without the fences, the indent of a list or quote it
    sits in removed."""

    line: int
    language: str
    code: str


class PageProse(NamedTuple):
    """A line of a page's text - not code, not a heading, not a table row."""

    line: int
    text: str


class DocsProblem(NamedTuple):
    """A place where a page breaks the docs standard."""

    page: str
    line: int
    message: str
