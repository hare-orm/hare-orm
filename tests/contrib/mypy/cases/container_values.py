"""The values of container fields as the mypy plugin types them - the whole container, its equality
filter taking the elements."""

from typing import assert_type

from tests.contrib.mypy.models import Crate


async def container_values() -> None:
    assert_type(await Crate.objects.values_list("labels", flat=True), list[list[str] | None])
    assert_type(await Crate.objects.values_list("grid", flat=True), list[list[list[int | None]]])
    assert_type(await Crate.objects.values_list("prices", flat=True), list[dict[str, int]])
    assert_type(await Crate.objects.values_list("point", flat=True), list[tuple[int, str]])
    row = (await Crate.objects.values("labels"))[0]
    assert_type(row["labels"], list[str] | None)
    # An element of an array is the element's type.
    assert_type(await Crate.objects.values_list("labels__0", flat=True), list[str | None])
    # The models' connection is SQLite, which has no column type for an array.
    await Crate.objects.filter(labels=["a"])  # E: Filter param 'labels': Crate.labels has no column type on the sqlite database
