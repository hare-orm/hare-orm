"""UUIDField(version=...): a UUID of another version is refused on every write path and in a filter; a primary
key of version 7 defaults to Uuid7.new; a migration writes the version."""

import uuid

import pytest

from hare import fields
from hare.contrib.test import hare_test_context
from hare.exceptions import ValidationError
from hare.fields.uuid7 import Uuid7
from hare.migrations.writer.import_manager import ImportManager
from hare.migrations.writer.migration_writer import MigrationWriter
from hare.models import Model

pytestmark = pytest.mark.sqlite_only

DB_URL = "sqlite+aiosqlite://:memory:"


class VersionedKey(Model):
    id = fields.UUIDField(primary_key=True, version=7)
    random_key = fields.UUIDField(version=4, null=True)


@pytest.mark.asyncio
async def test_a_primary_key_of_version_7_defaults_to_one():
    async with hare_test_context([__name__], db_url=DB_URL):
        row = await VersionedKey.objects.create()
    assert row.id.version == 7
    assert VersionedKey._meta.fields_map["id"].default == Uuid7.new


@pytest.mark.parametrize(
    "write",
    [
        lambda: VersionedKey.objects.create(id=uuid.uuid4()),
        lambda: VersionedKey.objects.bulk_create([VersionedKey(id=uuid.uuid4())]),
        lambda: VersionedKey.objects.create(random_key=Uuid7.new()),
        lambda: VersionedKey.objects.update(random_key=Uuid7.new()),
        lambda: VersionedKey.objects.filter(id=uuid.uuid4()).count(),
        lambda: VersionedKey.objects.filter(id__in=[uuid.uuid4()]).count(),
    ],
    ids=["create", "bulk_create", "create of another field", "update", "filter", "filter in"],
)
@pytest.mark.asyncio
async def test_a_uuid_of_another_version_is_refused(write):
    async with hare_test_context([__name__], db_url=DB_URL):
        await VersionedKey.objects.create()
        with pytest.raises(ValidationError, match="expected a version"):
            await write()


@pytest.mark.asyncio
async def test_save_and_bulk_update_refuse_it_too():
    async with hare_test_context([__name__], db_url=DB_URL):
        row = await VersionedKey.objects.create(random_key=uuid.uuid4())
        row.random_key = Uuid7.new()
        with pytest.raises(ValidationError, match="expected a version 4 UUID"):
            await row.save()
        with pytest.raises(ValidationError, match="expected a version 4 UUID"):
            await VersionedKey.objects.bulk_update([row], fields=["random_key"])


def test_both_ways_a_dialect_writes_a_uuid_check_it():
    field = VersionedKey._meta.fields_map["id"]
    with pytest.raises(ValidationError, match="expected a version 7 UUID"):
        field.to_db_value(str(uuid.uuid4()), VersionedKey)
    with pytest.raises(ValidationError, match="expected a version 7 UUID"):
        field.to_db_uuid(uuid.uuid4(), VersionedKey)
    assert field.to_db_uuid(key := Uuid7.new(), VersionedKey) == key


def test_a_null_passes_a_nullable_field():
    VersionedKey._meta.fields_map["random_key"].validate(None)


def test_a_migration_writes_the_version():
    path, _, kwargs = VersionedKey._meta.fields_map["random_key"].deconstruct()
    assert kwargs["version"] == 4
    imports = ImportManager()
    written = MigrationWriter.render_value(VersionedKey._meta.fields_map["id"], imports)
    assert "version=7" in written
    assert "default=" in written


def test_a_null_of_a_field_not_null_is_refused_as_a_null():
    with pytest.raises(ValidationError, match="must not be None"):
        VersionedKey._meta.fields_map["id"].validate(None)


def test_a_uuid_of_another_variant_is_refused_though_its_version_digit_matches():
    """The version and the variant bits are compared on the UUID's integer - a UUID of another variant than RFC
    9562's has no version, whatever its version digit holds."""
    field = VersionedKey._meta.fields_map["id"]
    other_variant = uuid.UUID(int=Uuid7.new().int & ~(0b11 << 62))
    assert other_variant.version is None
    with pytest.raises(ValidationError, match="expected a version 7 UUID"):
        field.validate(other_variant)
    field.validate(Uuid7.new())
