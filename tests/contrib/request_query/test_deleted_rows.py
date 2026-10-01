"""Meta.deleted = DeletedConfig(): a request sees soft-deleted rows when may_see_deleted() lets it."""

import pytest
import pytest_asyncio

from hare.contrib.request_query import DeletedConfig, InvalidRequestQuery, RequestQuery, RequestQueryForbidden
from hare.exceptions import ConfigurationError
from tests.contrib.request_query.models import Book, Note, NoteMark
from tests.contrib.request_query.queries import HiddenDeletedNoteQuery, NoteQuery, TrashedNoteQuery


@pytest_asyncio.fixture
async def notes(db_request_query):
    """Four notes, the second deleted; the first three marked "todo"."""
    for note_id, text in ((1, "one"), (2, "two"), (3, "three"), (4, "four")):
        note = await Note.objects.create(id=note_id, text=text)
        if note_id < 4:
            await NoteMark.objects.create(id=note_id, note=note, label="todo")
    await (await Note.objects.get(id=2)).delete()


def texts(notes: list[Note]) -> list[str]:
    return [note.text for note in notes]


@pytest.mark.asyncio
async def test_deleted_rows_by_the_parameter(notes):
    assert texts(await NoteQuery().fetch()) == ["one", "three", "four"]
    assert texts(await NoteQuery(deleted="exclude").fetch()) == ["one", "three", "four"]
    assert texts(await NoteQuery(deleted="include").fetch()) == ["one", "two", "three", "four"]
    assert texts(await NoteQuery(deleted="only").fetch()) == ["two"]
    assert await NoteQuery(deleted="include").count() == 4


@pytest.mark.asyncio
async def test_an_unknown_value_is_refused(notes):
    with pytest.raises(InvalidRequestQuery) as error:
        NoteQuery(deleted="all")
    assert error.value.errors[0]["loc"] == ["deleted"]


@pytest.mark.asyncio
async def test_may_see_deleted_governs_asking_for_them(notes):
    assert texts(await HiddenDeletedNoteQuery().fetch()) == ["one", "three", "four"]
    assert texts(await HiddenDeletedNoteQuery(deleted="exclude").fetch()) == ["one", "three", "four"]
    for deleted in ("include", "only"):
        with pytest.raises(RequestQueryForbidden) as error:
            await HiddenDeletedNoteQuery(deleted=deleted).fetch()
        assert error.value.parameter == "deleted"


@pytest.mark.asyncio
async def test_rows_taken_once_keep_the_deleted_ones(notes):
    counts = await NoteQuery(marks__label="todo", deleted="include").count_by("text")
    assert counts == {"text": {"one": 1, "three": 1, "two": 1}}


@pytest.mark.asyncio
async def test_the_parameter_can_be_named(notes):
    assert "deleted" not in TrashedNoteQuery.model_fields
    assert texts(await TrashedNoteQuery.from_query_string("trashed=only").fetch()) == ["two"]


def test_a_model_without_soft_delete_is_refused(db_request_query):
    class DeletedBookQuery(RequestQuery[Book]):
        class Meta:
            queryset = Book.objects.all()
            deleted = DeletedConfig()

    with pytest.raises(ConfigurationError, match="has no Meta.soft_delete_field"):
        DeletedBookQuery.get_declaration()
