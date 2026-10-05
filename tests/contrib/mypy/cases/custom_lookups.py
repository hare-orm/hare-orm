"""Lookups and transforms a project registers - in the models module and in a module of
``[tool.hare] mypy_imports``."""

from tests.contrib.mypy.models import Writer


def custom_lookups() -> None:
    Writer.objects.filter(rating__rated_between=(1, 5), rating__rated_above=3)
    Writer.objects.filter(rating__as_text="3", rating__as_text__startswith="1")
    Writer.objects.filter(rating__rated_between=("a", 5))  # E: Argument "rating__rated_between" to "filter" of "QuerySet" has incompatible type "tuple[str, int]"
    Writer.objects.filter(rating__rated_above="3")  # E: Argument "rating__rated_above" to "filter" of "QuerySet" has incompatible type "str"
    Writer.objects.filter(rating__as_text=3)  # E: Argument "rating__as_text" to "filter" of "QuerySet" has incompatible type "int"
    Writer.objects.filter(rating__rated_on_postgresql=(1, 2))  # E: Filter param 'rating__rated_on_postgresql': Writer.rating has no lookup 'rated_on_postgresql' on the sqlite database
