"""The names given to order_by(), only(), defer(), select_related() and bulk_update()."""

from tests.contrib.mypy.models import Note, Volume


async def accepted_names(volumes: list[Volume], notes: list[Note]) -> None:
    Volume.objects.order_by("-title", "?", "writer__name", "pk")
    Volume.objects.only("title", "writer__name")
    Volume.objects.defer("title", "price")
    Volume.objects.defer("writer")
    Note.objects.order_by("subject__type")
    Note.objects.only("id", "subject")
    Note.objects.defer("subject")
    await Note.objects.bulk_update(notes, fields=["subject"])
    Volume.objects.select_related("writer", "shelf", "writer__card")
    await Volume.objects.bulk_update(volumes, fields=["title", "price", "writer"])
    await Volume.objects.bulk_update(volumes, ["title"])


async def reported_names(volumes: list[Volume]) -> None:
    Volume.objects.order_by("-titel")  # E: titel
    Volume.objects.only("nope")  # E: only(): Volume has no field 'nope'
    Volume.objects.defer("nope")  # E: defer(): Volume has no field 'nope'
    Volume.objects.defer("labels")  # E: defer(): Volume.labels is a relation holding many rows - only direct fields and forward relations can be deferred
    Note.objects.order_by("subject")  # E: Unknown field subject for ordering: Note has no field 'subject'
    Volume.objects.select_related("labels")  # E: select_related() can't follow 'labels' on typing.Volume - it can hold many related rows
    Volume.objects.select_related("writer__nope")  # E: select_related() relation 'nope' for typing.Writer not found
    Volume.objects.select_related("writer__name")  # E: select_related() field 'name' on typing.Writer is not a relation
    await Volume.objects.bulk_update(volumes, fields=["labels"])  # E: bulk_update(): Volume.labels is a relation holding many rows - it can't be updated
    await Volume.objects.bulk_update(volumes, ["titel"])  # E: bulk_update(): Volume has no field 'titel'
