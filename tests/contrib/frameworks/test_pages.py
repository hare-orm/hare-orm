"""PageSchema[RowSchema]: the response model of a page of rows, read from the page itself."""

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from hare.contrib.frameworks import CursorPageSchema, PageSchema, ScrollPageSchema
from hare.contrib.request_query import CursorPage, Page, ScrollPage


@dataclass
class Book:
    id: int
    title: str


class BookSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str


ROWS = [Book(id=1, title="Alpha"), Book(id=2, title="Beta")]


def test_each_type_of_page_reads_its_rows_and_fields():
    page = PageSchema[BookSchema].model_validate(
        Page(result=ROWS, count=5, limit=2, offset=0, next="/books?offset=2", previous=None)
    )
    assert isinstance(page, PageSchema)
    assert page.model_dump() == {
        "count": 5,
        "limit": 2,
        "offset": 0,
        "next": "/books?offset=2",
        "previous": None,
        "result": [{"id": 1, "title": "Alpha"}, {"id": 2, "title": "Beta"}],
    }
    scroll = ScrollPageSchema[BookSchema].model_validate(
        ScrollPage(result=ROWS, limit=2, offset=0, next=None, previous=None)
    )
    assert isinstance(scroll, ScrollPageSchema)
    assert "count" not in type(scroll).model_fields
    cursor = CursorPageSchema[BookSchema].model_validate(
        CursorPage(result=ROWS, limit=2, next_cursor="abc", previous_cursor=None, next=None, previous=None)
    )
    assert isinstance(cursor, CursorPageSchema)
    assert cursor.next_cursor == "abc"


def test_a_page_schema_of_a_row_schema_is_one_class():
    assert PageSchema[BookSchema] is PageSchema[BookSchema]
    assert PageSchema[BookSchema] is not ScrollPageSchema[BookSchema]
