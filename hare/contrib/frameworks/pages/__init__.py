"""The pydantic schemas of a request query's pages - a handler's response model for a page of rows,
with the schema of one row as the parameter::

@app.get("/books", response_model=PageSchema[BookSchema])
async def list_books(books: ...) -> Page[Book]:
    return await books.page()

Each is read from the page's attributes, so a handler returns the page itself and its framework
validates it; the row schema reads each row's attributes (``from_attributes=True`` in its config).
"""

from __future__ import annotations

from hare.contrib.frameworks.pages.cursor_page_schema import CursorPageSchema
from hare.contrib.frameworks.pages.page_schema import PageSchema
from hare.contrib.frameworks.pages.scroll_page_schema import ScrollPageSchema

__all__ = [
    "PageSchema",
    "ScrollPageSchema",
    "CursorPageSchema",
]
