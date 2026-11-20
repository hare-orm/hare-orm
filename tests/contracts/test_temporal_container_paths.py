"""A temporal value extracted from a container keeps its lookups and ordering description."""

import datetime

import pytest

from hare import Model, fields
from hare.core.hare_context import HareContext
from hare.dialects.postgresql.fields import DateRangeField, Range
from hare.exceptions import FieldError, UnSupportedError
from hare.query.enums import Lookup, LookupValueShape
from hare.query.expressions import F, Value
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION


@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
@pytest.mark.parametrize("container", ["array", "range"])
async def test_extracted_temporal_values_keep_compound_lookups_and_ordering_metadata(
    contract_database, composite, container
):
    declaration = fields.ArrayField(fields.DatetimeField()) if container == "array" else DateRangeField()
    connection = contract_database.get_connection(CONTRACT_CONNECTION)
    try:
        declaration.get_column_type(connection.dialect)
    except UnSupportedError:
        pytest.skip("The dialect has no column type for this container")

    class Entry(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        if composite:
            region = fields.IntField(default=7)
            pk = fields.CompositePrimaryKey("region", "number")
        moments = declaration

    contract_database.register_live_models([Entry], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    source_dates = (datetime.date(2024, 12, 31), datetime.date(2023, 1, 1), datetime.date(2024, 1, 1))
    values = (
        [[datetime.datetime.combine(day, datetime.time(12), tzinfo=datetime.UTC)] for day in source_dates]
        if container == "array"
        else [Range(day, day + datetime.timedelta(days=1)) for day in source_dates]
    )
    await Entry.objects.bulk_create([Entry(number=number, moments=value) for number, value in enumerate(values, 1)])
    segment = "0" if container == "array" else "startswith"
    path = f"moments__{segment}"
    for repeat in (0, 1):
        for suffix, operand, expected in (
            ("year__gte", 2024, [1, 3]),
            ("year__in", [2023], [2]),
            ("year__not_in", [2023], [1, 3]),
            ("year__range", [2023, 2023], [2]),
        ):
            assert (
                await Entry.objects.filter(**{f"{path}__{suffix}": operand})
                .order_by("number")
                .values_list("number", flat=True)
                == expected
            )
        if container == "array":
            assert await Entry.objects.filter(**{f"{path}__date__in": [datetime.date(2024, 1, 1)]}).values_list(
                "number", flat=True
            ) == [3]
            assert await Entry.objects.filter(**{f"{path}__time": "12:00"}).order_by("number").values_list(
                "number", flat=True
            ) == [1, 2, 3]
        assert await Entry.objects.order_by(f"{path}__year", "number").values_list("number", flat=True) == [2, 1, 3]
        for descending in (False, True):
            name = ("-" if descending else "") + f"{path}__year"
            for info in (
                HareContext.require_current().apps.get_binding(Entry).get_ordering_info(name),
                Entry.objects.get_ordering_info(name),
            ):
                assert info.transforms == (segment, "year")
                assert info.descending is descending
        lookup_info = HareContext.require_current().apps.get_binding(Entry).get_lookup_info(f"{path}__year__in")
        assert lookup_info.transforms == (segment, "year")
        assert lookup_info.lookup == Lookup.IN
        assert lookup_info.value_shape == LookupValueShape.LIST
        assert lookup_info.value_type is int
        annotated = Entry.objects.annotate(score=Value(1))
        with pytest.raises(FieldError):
            annotated.get_ordering_info("score__banana")
        temporal_annotation = Entry.objects.annotate(chosen=F(path))
        with pytest.raises(FieldError):
            temporal_annotation.order_by("chosen__year").sql()
        with pytest.raises(FieldError):
            temporal_annotation.get_ordering_info("chosen__year")
