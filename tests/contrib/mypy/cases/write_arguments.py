"""The fields set by the model constructor, create() and update()."""

from datetime import datetime
from decimal import Decimal

from hare.query.expressions import F
from tests.contrib.mypy.models import Label, Note, Printing, Shelf, ShelfStatus, Volume, Writer


async def accepted_fields(writer: Writer, shelf: Shelf) -> None:
    Volume(title="a", price=Decimal(1), published=datetime(2020, 1, 1), writer=writer, shelf=None)
    Volume(writer_id=1, shelf_id=None)
    Writer(name="a", nickname=None, address={"city": "Paris", "street": "Rue"})
    Writer(name="b", address=None)
    Note(text="a", subject=writer)
    Printing(pk=(1, 2), copies=3)
    Shelf(label="b", status="closed")
    Shelf(label="c", status=ShelfStatus.OPEN)
    await Volume.objects.create(title="a", price=Decimal(1), published=datetime(2020, 1, 1), writer=writer)
    await Volume.objects.filter(id=1).update(title=F("writer__name"), price=Decimal(2), shelf=shelf)


async def reported_fields(writer: Writer, shelf: Shelf, label: Label) -> None:
    Volume(title=1)  # E: Argument "title" to "Volume" has incompatible type "int"; expected "str"
    Volume(nope=1)  # E: Volume has no field 'nope' to set
    Volume(labels=[label])  # E: Volume.labels is a relation holding many rows - it can't be set
    Volume(writer=shelf)  # E: Argument "writer" to "Volume" has incompatible type "Shelf"; expected "Writer"
    Volume(writer=None)  # E: Argument "writer" to "Volume" has incompatible type "None"; expected "Writer"
    Writer(address=5)  # E: Argument "address" to "Writer" has incompatible type "int"; expected "Address | None"
    Note(subject=shelf)  # E: Argument "subject" to "Note" has incompatible type "Shelf"
    await Volume.objects.create(titel="a")  # E: Volume has no field 'titel' to set
    await Volume.objects.filter(id=1).update(title=2)  # E: Argument "title" to "update" of "QuerySet" has incompatible type "int"
