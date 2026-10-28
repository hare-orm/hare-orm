import os

import pytest

from hare import Model, fields
from hare.contrib.test import hare_test_context, requires_features
from hare.core.registries import Registries
from hare.dialects.base.types.type_mapping import TypeMapping
from hare.dialects.base.types.type_registry import TypeRegistry


class UpperReadField(fields.CharField):
    def from_db_value(self, value):
        return None if value is None else value.upper()


class MappedReadField(fields.CharField):
    pass


class FieldReadKey(Model):
    id = fields.IntField(primary_key=True)
    name = UpperReadField(max_length=20, unique=True)


class DialectReadKey(Model):
    id = fields.IntField(primary_key=True)
    name = MappedReadField(max_length=20, unique=True)


class FieldReadPrimaryKey(Model):
    name = UpperReadField(max_length=20, primary_key=True)


class DialectReadPrimaryKey(Model):
    name = MappedReadField(max_length=20, primary_key=True)


@pytest.mark.database_independent
def test_native_key_type_rechecks_readers_registered_after_first_use():
    types = TypeRegistry()
    field = MappedReadField(max_length=20)
    assert types.get_native_key_type(field) is str
    types.register(MappedReadField, TypeMapping(to_python=UpperReadField.from_db_value))
    assert types.get_native_key_type(field) is None
    types.register(MappedReadField, TypeMapping())
    assert types.get_native_key_type(field) is str


@requires_features(supports_returning=True, supports_insert_conflicts=True)
@pytest.mark.parametrize(
    "model",
    [FieldReadKey, DialectReadKey, FieldReadPrimaryKey, DialectReadPrimaryKey],
    ids=["field-unique", "dialect-unique", "field-primary-key", "dialect-primary-key"],
)
@pytest.mark.asyncio
async def test_returned_rows_match_the_objects_after_read_conversion(db, model):
    async with hare_test_context([__name__], db_url=os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")):
        connection = model.get_connection()
        if model in (FieldReadKey, DialectReadKey) and not connection.features.guarantees_returning_order:
            pytest.skip("Matching generated keys requires ordered RETURNING")
        types = connection.dialect.types
        uses_mapping = model in (DialectReadKey, DialectReadPrimaryKey)
        previous_mapping = types.mappings.get(MappedReadField)
        if uses_mapping:
            types.register(MappedReadField, TypeMapping(to_python=UpperReadField.from_db_value))
        try:
            first, duplicate = model(name="same"), model(name="same")
            duplicate_key = duplicate.pk
            inserted = await model.objects.bulk_create(
                [first, duplicate], ignore_conflicts=True, on_conflict=["name"], returning=True
            )
            assert len(inserted) == 1 and inserted[0] is first
            assert first._saved_in_db and first.pk is not None
            assert not duplicate._saved_in_db and duplicate.pk == duplicate_key
            assert await model.objects.values("name") == [{"name": "SAME"}]
            assert (await model.objects.get()).pk == first.pk
        finally:
            if uses_mapping:
                if previous_mapping is None:
                    types.mappings.pop(MappedReadField)
                else:
                    types.mappings[MappedReadField] = previous_mapping
                Registries.changed()
