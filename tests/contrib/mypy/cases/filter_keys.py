"""Filter keys and their values: what the mypy plugin accepts and what it reports (``# E:``)."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from hare.query.expressions import F
from tests.contrib.mypy.models import Loan, Note, Printing, PrintingReview, Shelf, ShelfStatus, Volume, Writer


async def accepted_keys(writer: Writer, shelf: Shelf, printing: Printing, filters: dict[str, Any]) -> None:
    Volume.objects.filter(title="a", price=Decimal("1.5"), published=datetime(2020, 1, 1))
    Volume.objects.filter(published__year=2020, published__date=date(2020, 1, 1), price__gte=Decimal(1))
    Volume.objects.filter(writer=writer)
    Volume.objects.filter(writer=1, writer_id=1)
    Volume.objects.filter(writer__in=[writer, 2])
    Volume.objects.filter(shelf=None, shelf__isnull=True)
    Volume.objects.filter(shelf=shelf, shelf__label="top")
    Volume.objects.filter(labels__name="x", labels__name__icontains="y")
    Writer.objects.filter(volumes__title="x", card__issued=date(2020, 1, 1))
    Writer.objects.filter(nickname=None, nickname__startswith="J")
    Writer.objects.filter(address__city="Paris")
    Volume.objects.filter(title__in=["a", "b"], title__range=("a", None))
    Volume.objects.filter(price__range=[Decimal(1), Decimal(2)])
    Volume.objects.filter(title=F("writer__name"))
    Volume.objects.filter(writer__in=Writer.objects.filter(name="a"))
    PrintingReview.objects.filter(printing=printing)
    PrintingReview.objects.filter(printing=(1, 2), printing__in=[(1, 2), printing])
    Printing.objects.filter(pk=(1, 2))
    Note.objects.filter(subject=writer, subject__type="volume")
    Shelf.objects.filter(status=ShelfStatus.OPEN)
    Shelf.objects.filter(status="open", status__in=["open", ShelfStatus.CLOSED])
    Writer.objects.filter(address__contains={"city": "Paris"}, address__has_key="city")
    Volume.objects.exclude(title="a").filter(**filters)
    await Volume.objects.get(title="a")
    await Volume.objects.get(title="a", does_not_exist_exception=None)
    await Volume.objects.get_or_create(title="a", defaults={"price": Decimal(1)})
    writer.volumes.filter(title="x")
    Volume.objects.filter(writer__card__issued__year=2020, shelf__volumes__writer__name="a")
    Loan.objects.filter(reader=writer, reader__name="a")


async def reported_keys(writer: Writer, shelf: Shelf) -> None:
    Volume.objects.filter(titel="a")  # E: Unknown filter param 'titel': Volume has no field 'titel'
    Volume.objects.filter(title__nope="a")  # E: Volume.title has no lookup 'nope'
    Volume.objects.exclude(nope=1)  # E: Unknown filter param 'nope'
    writer.volumes.filter(titl="x")  # E: Unknown filter param 'titl'
    await Volume.objects.get(nope=1)  # E: Unknown filter param 'nope'
    Volume.objects.filter(title=1)  # E: Argument "title" to "filter" of "QuerySet" has incompatible type "int"
    Volume.objects.filter(title=None)  # E: Argument "title" to "filter" of "QuerySet" has incompatible type "None"
    Volume.objects.filter(published__year="2020")  # E: Argument "published__year" to "filter" of "QuerySet" has incompatible type "str"
    Volume.objects.filter(writer=shelf)  # E: Argument "writer" to "filter" of "QuerySet" has incompatible type "Shelf"
    Volume.objects.filter(title__in=[1])  # E: List item 0 has incompatible type "int"; expected "str"
    Volume.objects.filter(shelf__isnull="yes")  # E: Argument "shelf__isnull" to "filter" of "QuerySet" has incompatible type "str"
    Note.objects.filter(subject__type="label")  # E: Argument "subject__type" to "filter" of "QuerySet" has incompatible type "Literal['label']"
    Note.objects.filter(subject=shelf)  # E: Argument "subject" to "filter" of "QuerySet" has incompatible type "Shelf"
    Loan.objects.filter(reader=shelf)  # E: Argument "reader" to "filter" of "QuerySet" has incompatible type "Shelf"
    Volume.objects.filter(writer__card__issued__year="2020")  # E: Argument "writer__card__issued__year" to "filter" of "QuerySet" has incompatible type "str"
    await Volume.objects.get_or_create(titel="a")  # E: Unknown filter param 'titel'
    await Volume.objects.update_or_create(defaults={"title": "b"}, nope=1)  # E: Unknown filter param 'nope'
