"""Annotations: their names are filter keys and orderings, their types follow their expressions."""

from decimal import Decimal
from typing import Any, assert_type

from hare.query.expressions import Value
from hare.query.functions import Count, Sum
from hare.query.queryset import QuerySet
from tests.contrib.mypy.models import Volume, Writer


async def annotated(writer: Writer) -> None:
    queryset = Volume.objects.annotate(label_count=Count("labels"), total=Sum("price"), tag=Value("x"))
    queryset.filter(label_count__gte=2, total=Decimal(1), tag="y").order_by("-label_count")
    plain: QuerySet[Volume] = queryset
    rows = await queryset.values("label_count", "total", "tag")
    assert_type(rows[0]["label_count"], int)
    assert_type(rows[0]["total"], Decimal | None)
    assert_type(rows[0]["tag"], str)
    every_name = await queryset.values()
    assert_type(every_name[0]["label_count"], int)
    Volume.objects.alias(label_count=Count("labels")).filter(label_count=1)
    writer.volumes.annotate(label_count=Count("labels")).filter(label_count=1)
    Volume.selection.annotate(label_count=Count("labels")).priced().filter(label_count=1)
    Volume.selection.annotate(label_count=Count("labels")).values("title", "label_count").filter(label_count=1)
    Volume.selection.annotate(label_count=Count("labels")).values_list("title", flat=True).filter(label_count=1)
    queryset.filter(label_count="many")  # E: Argument "label_count" to "filter" of "QuerySet" has incompatible type "str"
    Volume.objects.filter(label_count=1)  # E: Unknown filter param 'label_count'
    Volume.selection.priced().filter(label_count=1)  # E: Unknown filter param 'label_count'
    aliased = await Volume.objects.alias(label_count=Count("labels")).values()
    aliased[0]["label_count"]  # E: "label_count" is not a valid TypedDict key
    plain.filter(nope=1)  # E: Unknown filter param 'nope'


def helper(queryset: QuerySet[Volume, Volume, Any]) -> None:
    queryset.filter(computed=1).order_by("computed")
