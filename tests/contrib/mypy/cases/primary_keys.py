"""The pk of a model instance as the mypy plugin types it."""

from typing import assert_type

from tests.contrib.mypy.models import Printing, Writer, WriterCard


def primary_keys(writer: Writer, card: WriterCard, printing: Printing) -> None:
    assert_type(writer.pk, int)
    # pk of a model keyed by a relation is the relation's key.
    assert_type(card.pk, int)
    assert_type(printing.pk, tuple[int, int])
    writer.pk = None
    printing.pk = (1, 2)
    writer.pk = "1"  # E: Incompatible types in assignment (expression has type "str", variable has type "int | None")
