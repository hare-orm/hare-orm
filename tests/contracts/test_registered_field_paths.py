"""Registered paths use one declaration before a field's built-in path hook."""

from functools import partial

import pytest

from hare import Model, fields
from hare.core.registries import Registries
from hare.dialects.clickhouse.fields import DynamicField, LowCardinalityField, VariantField
from hare.dialects.dialect_registry import DialectRegistry
from hare.dialects.postgresql.fields import HStoreField, IntMultiRangeField, IntRangeField, LtreeField
from hare.exceptions import UnSupportedError
from hare.query.expressions import F
from hare.query.filters.lookups.field_transforms import FieldTransforms
from hare.query.functions import Count
from hare.sql.terms.containers import ArrayElementTerm
from hare.sql.terms.functions import Function
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION


@pytest.mark.database_independent
@pytest.mark.parametrize(
    "declaration",
    [
        fields.TextField(),
        fields.ArrayField(fields.IntField()),
        fields.TupleField([fields.IntField()]),
        fields.MapField(fields.IntField(), fields.IntField()),
        fields.MapField(fields.CharField(max_length=20), fields.IntField()),
        fields.NestedField({"amount": fields.IntField()}),
        IntRangeField(),
        IntMultiRangeField(),
        LtreeField(),
        HStoreField(),
        LowCardinalityField(fields.TextField()),
        VariantField([fields.IntField(), fields.TextField()]),
        DynamicField(),
    ],
    ids=lambda declaration: type(declaration).__name__,
)
def test_registered_paths_and_extension_metadata_precede_type_specific_paths(declaration):
    DialectRegistry.load()
    previous_snapshot = Registries.snapshot
    output_field = fields.IntField()
    build_term = partial(Function, "ABS")
    type(declaration).register_transform(
        "registered_amount", lambda field: (build_term, output_field), required_extension="amount_extension"
    )
    try:
        for repeat in (0, 1):
            transforms, output, remaining = FieldTransforms.get_path(declaration, ["registered_amount"])
            assert transforms == [build_term]
            assert output is output_field
            assert remaining == []
            assert FieldTransforms.get_required_extension(declaration, "registered_amount") == "amount_extension"
            assert (
                FieldTransforms.get_path_required_extension(declaration, ["registered_amount"]) == "amount_extension"
            )
    finally:
        Registries.snapshot = previous_snapshot
        Registries.changed()


@pytest.mark.database_independent
def test_registered_paths_use_subclass_precedence_and_generated_output_types():
    DialectRegistry.load()
    parent = type("ParentArray", (fields.ArrayField,), {})
    child = type("ChildArray", (parent,), {})
    previous_snapshot = Registries.snapshot
    output_field = fields.IntField()
    parent_term = partial(Function, "ABS")
    child_term = partial(Function, "ROUND")
    parent.register_transform("amount", lambda field: (parent_term, output_field))
    try:
        declaration = child(fields.IntField())
        assert FieldTransforms.get_transform(declaration, "amount") == (parent_term, output_field)
        child.register_transform("amount", lambda field: (child_term, output_field))
        generated = fields.GeneratedField(expression=F("source"), output_field=declaration, stored=True)
        assert FieldTransforms.get_transform(declaration, "amount") == (child_term, output_field)
        assert FieldTransforms.get_transform(generated, "amount") == (child_term, output_field)
        assert FieldTransforms.get_transform(declaration, "1")[1] is declaration.base_field
    finally:
        Registries.snapshot = previous_snapshot
        Registries.changed()


@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
async def test_registered_array_paths_apply_to_projection_filters_and_ordering(contract_database, composite):
    DialectRegistry.load()
    connection = contract_database.get_connection(CONTRACT_CONNECTION)
    numbers_field = fields.ArrayField(fields.IntField())
    try:
        numbers_field.get_column_type(connection.dialect)
    except UnSupportedError:
        pytest.skip("The dialect has no array column type")
    previous_snapshot = Registries.snapshot
    fields.ArrayField.register_transform(
        "first", lambda field: (partial(ArrayElementTerm, index=0, element_field=field.base_field), field.base_field)
    )
    fields.ArrayField.register_transform(
        "0", lambda field: (partial(ArrayElementTerm, index=1, element_field=field.base_field), field.base_field)
    )
    try:

        class Entry(Model):
            number = fields.IntField(primary_key=not composite, generated=False)
            if composite:
                region = fields.IntField(default=7)
                pk = fields.CompositePrimaryKey("region", "number")
            numbers = numbers_field

        contract_database.register_live_models([Entry], CONTRACT_APP, CONTRACT_CONNECTION)
        await contract_database.generate_schemas()
        await Entry.objects.bulk_create(
            [Entry(number=1, numbers=[11, 40]), Entry(number=2, numbers=[22, 30]), Entry(number=3, numbers=[33, 20])]
        )
        for repeat in (0, 1):
            assert await Entry.objects.order_by("number").values_list("numbers__first", flat=True) == [11, 22, 33]
            assert await Entry.objects.filter(numbers__first=22).values_list("number", flat=True) == [2]
            assert await Entry.objects.order_by("numbers__first").values_list("number", flat=True) == [1, 2, 3]
            assert await Entry.objects.order_by("numbers__0").values_list("number", flat=True) == [3, 2, 1]
            assert await Entry.objects.order_by("number").values_list("numbers__1", flat=True) == [40, 30, 20]
            assert await Entry.objects.order_by("number").annotate(amount=F("numbers__first")).values_list(
                "amount", flat=True
            ) == [11, 22, 33]
            assert [
                value
                async for value in Entry.objects.order_by("number")
                .values_list("numbers__first", flat=True)
                .iterator(chunk_size=1)
            ] == [11, 22, 33]
            assert await Entry.objects.annotate(total=Count("number")).group_by("numbers__len").order_by(
                "numbers__len"
            ).values_list("numbers__len", "total") == [(2, 3)]
    finally:
        Registries.snapshot = previous_snapshot
        Registries.changed()
