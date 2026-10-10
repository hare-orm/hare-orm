"""The names given to order_by(), only(), defer(), select_related() and bulk_update()."""

from tests.contrib.mypy.models import Volume


async def accepted_names(volumes: list[Volume]) -> None:
    Volume.objects.order_by("-title", "?", "writer__name", "pk")
    Volume.objects.only("title", "writer__name")
    Volume.objects.defer("title", "price")
    Volume.objects.select_related("writer", "shelf", "writer__card")
    await Volume.objects.bulk_update(volumes, fields=["title", "price", "writer"])
    await Volume.objects.bulk_update(volumes, ["title"])


async def reported_names(volumes: list[Volume]) -> None:
    Volume.objects.order_by("-titel")  # E: titel
    Volume.objects.only("nope")  # E: only(): Volume has no field 'nope'
    Volume.objects.defer("nope")  # E: defer(): Volume has no field 'nope'
    Volume.objects.defer("writer")  # E: defer(): Volume.writer is a relation - only direct fields can be deferred
    Volume.objects.select_related("labels")  # E: select_related() can't follow 'labels' on typing.Volume - it can hold many related rows
    Volume.objects.select_related("writer__nope")  # E: select_related() relation 'nope' for typing.Writer not found
    Volume.objects.select_related("writer__name")  # E: select_related() field 'name' on typing.Writer is not a relation
    await Volume.objects.bulk_update(volumes, fields=["labels"])  # E: bulk_update(): Volume.labels is a relation holding many rows - it can't be updated
    await Volume.objects.bulk_update(volumes, ["titel"])  # E: bulk_update(): Volume has no field 'titel'
