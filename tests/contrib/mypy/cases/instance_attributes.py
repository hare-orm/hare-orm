"""The attributes of a model instance as the mypy plugin types them - read from the model's fields."""

from typing import Any, assert_type

from tests.contrib.mypy.models import Crate, Note, PrintingReview, Volume, Writer, WriterCard


def instance_attributes(
    volume: Volume, card: WriterCard, review: PrintingReview, note: Note, crate: Crate, writer: Writer
) -> None:
    # A relation's key column.
    assert_type(volume.writer_id, int)
    assert_type(volume.shelf_id, int | None)
    assert_type(card.writer_id, int)
    assert_type(review.printing_edition, int)
    # A nullable field - the class doesn't say so for a TextField.
    assert_type(crate.memo, str | None)
    assert_type(writer.nickname, str | None)
    assert_type(crate.labels, list[str] | None)
    assert_type(crate.prices, dict[str, int])
    # A JSON document is any value - JSONField(default=dict) needs no annotation.
    assert_type(crate.document, Any)
    assert_type(note.subject, Volume | Writer)
    note.subject = {"type": "writer", "id": 1}
    note.subject = writer
    note.subject = 1  # E: Incompatible types in assignment (expression has type "int", variable has type "Volume | Writer | dict[str, Any] | None")
    crate.memo = None
    crate.prices = None  # E: Incompatible types in assignment (expression has type "None", variable has type "dict[str, int]")
