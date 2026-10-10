"""QuerySet methods the dialect of the models' connection registered - declared with the parameters of
their call; another dialect's are unknown."""

from typing import assert_type

from hare.query.queryset.declarations import NoAnnotations
from hare.query.queryset.queryset import QuerySet
from tests.contrib.mypy.models import Writer


def dialect_methods() -> None:
    assert_type(Writer.objects.all().first_rows("c"), QuerySet[Writer, Writer, NoAnnotations])
    Writer.objects.all().first_rows("c", inclusive=True)
    Writer.objects.all().first_rows()  # E: Missing positional argument "name_below" in call to "first_rows" of "QuerySet"
    Writer.objects.all().first_rows("c", "d")  # E: Too many positional arguments for "first_rows" of "QuerySet"
    Writer.objects.all().only_on_postgresql("c")  # E: "QuerySet[Writer, Writer, NoAnnotations]" has no attribute "only_on_postgresql"
