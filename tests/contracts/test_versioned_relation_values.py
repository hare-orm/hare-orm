"""Version copying expands logical relation names through the shared stored-field contract."""

import sys
from types import ModuleType

import pytest
import pytest_asyncio

from hare import Model, fields
from hare.contrib.test import hare_test_context
from hare.contrib.versioning import VersionedModel
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION
from tests.utils.database_under_test import DatabaseUnderTest


@pytest.mark.asyncio
async def test_a_new_version_preserves_copied_python_values_and_converts_overrides(contract_database):
    class Incremented(fields.IntField):
        def to_python(self, value):
            return super().to_python(value) + 1

    class Reading(VersionedModel):
        amount = Incremented()
        label = fields.CharField(max_length=20)

        class Meta:
            table = "version_copy_reading"

    contract_database.register_live_models([Reading], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    source = await Reading.objects.create(amount=7, label="first")

    assert source.get_new_version(label="copy").amount == 8
    assert source.get_new_version(amount=10, label="override").amount == 11
    created = await source.create_new_version(label="copy")
    assert created.amount == 8
    rows = await contract_database.get_connection().execute_dicts(
        "SELECT amount FROM version_copy_reading ORDER BY version"
    )
    assert rows == [{"amount": 8}, {"amount": 8}]


@pytest_asyncio.fixture
async def version_relations(monkeypatch):
    class SingleTarget(Model):
        id = fields.IntField(primary_key=True, generated=False)

    class CompositeTarget(Model):
        area = fields.TextField()
        number = fields.IntField()
        pk = fields.CompositePrimaryKey("area", "number")

    class Record(VersionedModel):
        name = fields.TextField()
        target = fields.GenericForeignKeyField(
            {"single": SingleTarget, "composite": CompositeTarget},
            null=True,
            related_name="versions",
            db_constraint=False,
        )

    class ResetRecord(VersionedModel):
        name = fields.TextField()
        target = fields.GenericForeignKeyField(
            {"single": SingleTarget, "composite": CompositeTarget},
            null=True,
            related_name="reset_versions",
            db_constraint=False,
        )

        class Meta:
            new_version_excluded_fields = ("target",)

    module = ModuleType("tests._versioned_relation_values")
    module.__models__ = [SingleTarget, CompositeTarget, Record, ResetRecord]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    async with hare_test_context([module.__name__], DatabaseUnderTest.get_url()):
        single = await SingleTarget.objects.create(id=1)
        composite = await CompositeTarget.objects.create(area="", number=0)
        yield single, composite, Record, ResetRecord


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", (False, True), ids=("full", "partial"))
@pytest.mark.parametrize("input_form", ("model", "tagged", "null"))
@pytest.mark.parametrize("save", (False, True), ids=("build", "save"))
async def test_new_versions_replace_every_column_of_a_generic_relation(version_relations, partial, input_form, save):
    single, composite, record_model, _reset_model = version_relations
    original = await record_model.objects.create(name="original", target=single)
    source = await record_model.objects.only("id", "version", "name").get(pk=original.pk) if partial else original
    value = {"model": composite, "tagged": {"type": "composite", "area": "", "number": 0}, "null": None}[input_form]
    revised = await source.create_new_version(target=value) if save else source.get_new_version(target=value)
    expected = (None, None, None) if input_form == "null" else (None, "", 0)
    assert revised.pk == (original.id, 2)
    assert (revised.single_id, revised.composite_area, revised.composite_number) == expected
    assert (original.single_id, original.composite_area, original.composite_number) == (1, None, None)
    rows = (
        await record_model.objects.filter(id=original.id)
        .order_by("version")
        .values_list("version", "single_id", "composite_area", "composite_number")
    )
    expected_rows = [(1, 1, None, None)]
    if save:
        expected_rows.append((2, *expected))
    assert rows == expected_rows


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", (False, True), ids=("full", "partial"))
@pytest.mark.parametrize("target_shape", ("single", "composite"))
async def test_excluding_a_generic_relation_resets_all_branches_on_a_new_version(
    version_relations, partial, target_shape
):
    single, composite, _record_model, reset_model = version_relations
    target = single if target_shape == "single" else composite
    original = await reset_model.objects.create(name="original", target=target)
    source = await reset_model.objects.only("id", "version", "name").get(pk=original.pk) if partial else original
    revised = await source.create_new_version()
    assert revised.pk == (original.id, 2)
    assert (revised.single_id, revised.composite_area, revised.composite_number) == (None, None, None)
    first = (1, None, None) if target_shape == "single" else (None, "", 0)
    assert await reset_model.objects.filter(id=original.id).order_by("version").values_list(
        "version", "single_id", "composite_area", "composite_number"
    ) == [(1, *first), (2, None, None, None)]
