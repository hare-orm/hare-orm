"""
Integration tests for db_default field behavior during INSERT.

These tests verify that fields with db_default:
1. Get DatabaseDefault sentinel set when no value provided
2. Emit DEFAULT keyword in INSERT SQL
3. Have DB-applied defaults fetched back via RETURNING or SELECT
4. Work correctly when user provides explicit values
5. Work with bulk_create (resolving to literal values)
6. Work with save()/update (skipping DatabaseDefault fields)
"""

import datetime
import re
from decimal import Decimal

import pydantic
import pytest

from hare import fields
from hare.contrib.pydantic import pydantic_model_creator
from hare.dialects.dialect_registry import DialectRegistry
from hare.exceptions import (
    QueryError,
)
from hare.fields.database_default import DatabaseDefault
from hare.time import UTC
from tests.testmodels import DefaultModel, NoFetchDefaultModel, SqlDefaultModel
from tests.utils.timezone_context import override_timezone


class TestDatabaseDefaultSentinel:
    def test_repr(self):
        f = fields.IntField(db_default=1)
        f.model_field_name = "test_field"
        dd = DatabaseDefault(f)
        assert "DatabaseDefault" in repr(dd)

    def test_bool_is_false(self):
        f = fields.IntField(db_default=1)
        dd = DatabaseDefault(f)
        assert bool(dd) is False

    def test_str(self):
        f = fields.IntField(db_default=1)
        dd = DatabaseDefault(f)
        assert str(dd) == "<DB_DEFAULT>"


class TestFieldRequired:
    def test_required_false_with_db_default(self):
        f = fields.CharField(max_length=50, db_default="")
        assert f.required is False


class TestGetDbDefaultValue:
    def test_returns_database_default_when_has_db_default(self):
        f = fields.IntField(db_default=42)
        result = f.get_db_default_value()
        assert isinstance(result, DatabaseDefault)
        assert result.field is f

    def test_returns_none_when_no_db_default(self):
        f = fields.IntField()
        result = f.get_db_default_value()
        assert result is None


class TestModelInitDbDefault:
    @pytest.mark.asyncio
    async def test_init_sets_database_default(self, db):

        instance = DefaultModel()
        assert isinstance(instance.int_default, DatabaseDefault)
        assert isinstance(instance.char_default, DatabaseDefault)
        assert isinstance(instance.bool_default, DatabaseDefault)
        assert isinstance(instance.float_default, DatabaseDefault)

    @pytest.mark.asyncio
    async def test_init_with_explicit_value(self, db):

        instance = DefaultModel(int_default=42)
        assert instance.int_default == 42
        assert isinstance(instance.char_default, DatabaseDefault)


class TestConstructWithDbDefault:
    @pytest.mark.asyncio
    async def test_construct_sets_database_default(self, db):

        instance = DefaultModel.construct()
        assert isinstance(instance.int_default, DatabaseDefault)
        assert isinstance(instance.char_default, DatabaseDefault)


class TestCreateWithDbDefault:
    @pytest.mark.asyncio
    async def test_create_no_args_applies_db_defaults_and_persists(self, db):

        instance = await DefaultModel.objects.create()
        assert instance.pk is not None
        assert instance.int_default == 1
        assert instance.float_default == 1.5
        assert instance.bool_default is True
        assert instance.char_default == "hare"

        refreshed = await DefaultModel.objects.get(pk=instance.pk)
        assert refreshed.int_default == 1
        assert refreshed.char_default == "hare"
        assert refreshed.bool_default is True
        assert refreshed.float_default == 1.5

    @pytest.mark.asyncio
    async def test_create_with_partial_override(self, db):

        instance = await DefaultModel.objects.create(int_default=99, char_default="custom")
        assert instance.int_default == 99
        assert instance.char_default == "custom"
        assert instance.bool_default is True

        instance2 = await DefaultModel.objects.create(int_default=42)
        assert instance2.int_default == 42
        assert instance2.char_default == "hare"

    @pytest.mark.asyncio
    async def test_create_all_explicit_values(self, db):
        """When all values are provided, cached query path is used (no DEFAULT keyword)."""
        instance = await DefaultModel.objects.create(
            int_default=10,
            float_default=2.5,
            decimal_default=Decimal("3.14"),
            bool_default=False,
            char_default="all_set",
            date_default=datetime.date(2024, 1, 1),
            datetime_default=datetime.datetime(2024, 1, 1, tzinfo=UTC),
        )
        assert instance.int_default == 10
        assert instance.float_default == 2.5
        assert instance.char_default == "all_set"
        assert instance.bool_default is False


class TestBulkCreateWithDbDefault:
    @pytest.mark.asyncio
    async def test_bulk_create_all_defaults(self, db):

        instances = [DefaultModel() for _ in range(3)]
        await DefaultModel.objects.bulk_create(instances)
        all_inst = await DefaultModel.objects.all()
        assert len(all_inst) == 3
        for inst in all_inst:
            assert inst.int_default == 1
            assert inst.char_default == "hare"

    @pytest.mark.asyncio
    async def test_bulk_create_all_explicit_values(self, db):
        """When all instances provide explicit values for a db_default field, column is included."""

        inst1 = DefaultModel(int_default=99)
        inst2 = DefaultModel(int_default=77)
        await DefaultModel.objects.bulk_create([inst1, inst2])
        all_inst = await DefaultModel.objects.all().order_by("id")
        assert all_inst[0].int_default == 99
        assert all_inst[1].int_default == 77

    @pytest.mark.asyncio
    async def test_bulk_create_mixed_raises(self, db):
        """Mixed usage (some default, some explicit) for same field raises OperationalError."""
        inst1 = DefaultModel(int_default=99)
        inst2 = DefaultModel()  # int_default is DatabaseDefault
        with pytest.raises(QueryError, match="Cannot use bulk_create"):
            await DefaultModel.objects.bulk_create([inst1, inst2])

    @pytest.mark.asyncio
    async def test_bulk_create_sql_default_all_defaults_omits_column(self, db):
        """bulk_create with SqlDefault fields where all use default should omit the column."""
        instances = [SqlDefaultModel(name="test1"), SqlDefaultModel(name="test2")]
        await SqlDefaultModel.objects.bulk_create(instances)
        all_inst = await SqlDefaultModel.objects.all()
        assert len(all_inst) == 2
        for inst in all_inst:
            assert inst.created_at is not None
            assert inst.counter == 0

    @pytest.mark.asyncio
    async def test_bulk_create_of_a_model_of_only_database_defaults(self, db):
        """bulk_create of instances giving no value for any db_default field works."""
        instances = [NoFetchDefaultModel() for _ in range(3)]
        await NoFetchDefaultModel.objects.bulk_create(instances)
        all_inst = await NoFetchDefaultModel.objects.all()
        assert len(all_inst) == 3
        for inst in all_inst:
            assert inst.int_val == 1
            assert inst.char_val == "test"


class TestSaveUpdateWithDbDefault:
    @pytest.mark.asyncio
    async def test_update_skips_database_default_fields(self, db):

        instance = await DefaultModel.objects.create()

        # Targeted update with update_fields
        instance.int_default = 42
        await instance.save(update_fields=["int_default"])
        refreshed = await DefaultModel.objects.get(pk=instance.pk)
        assert refreshed.int_default == 42
        assert refreshed.char_default == "hare"

        # Full save() should also skip DatabaseDefault fields
        refreshed.int_default = 99
        await refreshed.save()
        final = await DefaultModel.objects.get(pk=instance.pk)
        assert final.int_default == 99
        assert final.char_default == "hare"


class TestNoFetchDbDefaults:
    @pytest.mark.asyncio
    async def test_create_of_a_model_of_only_database_defaults(self, db):
        """The INSERT emits DEFAULT for every db_default field and the instance gets the values
        the database gave them - through RETURNING, or a SELECT where there is none."""
        instance = await NoFetchDefaultModel.objects.create()
        assert instance.pk is not None
        assert instance.int_val == 1
        assert instance.char_val == "test"

        # A fresh SELECT always returns the real values
        refreshed = await NoFetchDefaultModel.objects.get(pk=instance.pk)
        assert refreshed.int_val == 1
        assert refreshed.char_val == "test"


class TestNowDbDefaultPrecision:
    @pytest.mark.asyncio
    async def test_sqlite_preserves_sub_second_precision(self, db):
        """Now() db_default must not lose sub-second precision on SQLite, and must store exactly
        the text a Python-side write of the same instant produces (6-digit fraction, omitted
        only when zero, "+00:00" suffix) - otherwise exact/__in/range filters miss the row."""
        conn = db.get_connection()
        if conn.dialect.name != "sqlite":
            pytest.skip("SQLite-specific precision check")

        instance = await SqlDefaultModel.objects.create(name="precision-check")
        _, rows = await conn.execute(f'SELECT created_at FROM "sql_default_model" WHERE id = {instance.pk}')
        raw_value = rows[0]["created_at"]
        assert isinstance(raw_value, str)
        assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(\.\d{3}000)?\+00:00", raw_value), raw_value
        fetched = await SqlDefaultModel.objects.get(pk=instance.pk)
        assert raw_value == fetched.created_at.astimezone(UTC).isoformat(" ")

    @pytest.mark.asyncio
    async def test_sql_default_expression_fills_the_column(self, db):
        """An operator expression - SQLite's DEFAULT only takes one in parentheses."""
        instance = await SqlDefaultModel.objects.create(name="expression")

        assert instance.computed == 42
        assert (await SqlDefaultModel.objects.get(pk=instance.pk)).computed == 42

    @pytest.mark.asyncio
    async def test_now_default_row_matched_by_its_own_value(self, db):
        """Equality, __in, __gte/__lte filters and get_or_create() by the value read back from a
        Now()-filled row must find that row."""
        instance = await SqlDefaultModel.objects.create(name="now-match")
        created_at = (await SqlDefaultModel.objects.get(pk=instance.pk)).created_at

        rows_for_instance = SqlDefaultModel.objects.filter(pk=instance.pk)
        assert await rows_for_instance.filter(created_at=created_at).count() == 1
        assert await rows_for_instance.filter(created_at__in=[created_at]).count() == 1
        assert await rows_for_instance.filter(created_at__gte=created_at).count() == 1
        assert await rows_for_instance.filter(created_at__lte=created_at).count() == 1
        _, created = await SqlDefaultModel.objects.get_or_create(name="now-match", created_at=created_at)
        assert created is False

    @pytest.mark.asyncio
    async def test_sqlite_now_renders_naive_local_text_with_use_tz_false(self, db):
        """Under use_timezone=False every Python-side DatetimeField value is naive local text - Now()
        must render the same, not UTC with a "+00:00" suffix."""
        from hare.fields.db_defaults import Now

        conn = db.get_connection()
        if conn.dialect.name != "sqlite":
            pytest.skip("SQLite-specific Now() rendering check")

        with override_timezone(use_timezone=False):
            now_sql = Now().get_sql(DialectRegistry.get_dialect("sqlite"))
        before = datetime.datetime.now() - datetime.timedelta(seconds=5)
        _, rows = await conn.execute(f"SELECT {now_sql} AS now_value")
        after = datetime.datetime.now() + datetime.timedelta(seconds=5)
        raw_value = rows[0]["now_value"]
        assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(\.\d{3}000)?", raw_value), raw_value
        assert before <= datetime.datetime.fromisoformat(raw_value) <= after


class TestNowDbDefaultTimezone:
    @pytest.mark.asyncio
    async def test_sqlite_non_utc_configured_zone_reads_back_as_the_real_instant(self, db):
        """Now() db_default on SQLite used to read back shifted by the configured zone's own
        UTC offset in any non-UTC zone - the raw `strftime('%Y-%m-%d %H:%M:%f', 'now')` string
        carried no offset of its own, so DatetimeField.get_python_value_with_timezone treated it as
        ALREADY being in the configured zone (Timezone.make_aware()) instead of converting it
        FROM UTC into that zone. Only correct by coincidence when the configured zone was UTC
        itself, which is why the suite running entirely in UTC never caught it."""
        conn = db.get_connection()
        if conn.dialect.name != "sqlite":
            pytest.skip("SQLite-specific timezone check")

        before_utc = datetime.datetime.now(datetime.UTC)
        with override_timezone(use_timezone=True, timezone="Europe/Moscow"):
            instance = await SqlDefaultModel.objects.create(name="tz-check")
        after_utc = datetime.datetime.now(datetime.UTC)

        created_moscow = instance.created_at
        assert created_moscow.tzinfo is not None

        # created_at must be within a couple seconds of "now" as seen in Moscow time - the buggy
        # version instead returned the raw UTC wall-clock digits mislabeled as Moscow time, off
        # by the zone's own +03:00 offset (~3 hours), never within a couple seconds.
        lower = before_utc.astimezone(created_moscow.tzinfo) - datetime.timedelta(seconds=5)
        upper = after_utc.astimezone(created_moscow.tzinfo) + datetime.timedelta(seconds=5)
        assert lower <= created_moscow <= upper, f"{created_moscow} not within [{lower}, {upper}]"


class TestMetaInfo:
    @pytest.mark.asyncio
    async def test_db_default_meta_attributes(self, db):
        meta = DefaultModel._meta
        assert len(meta.db_default_db_columns) > 0
        assert "int_default" in meta.db_default_db_columns
        assert "char_default" in meta.db_default_db_columns


class TestPydanticDbDefault:
    @pytest.mark.asyncio
    async def test_pydantic_no_fetch_requires_refresh(self, db):
        PydanticNoFetch = pydantic_model_creator(NoFetchDefaultModel)

        instance = await NoFetchDefaultModel.objects.create()
        conn = instance._meta.connection

        if not conn.features.supports_returning:
            # Without RETURNING, unfetched db_default fields are DatabaseDefault sentinels
            # and pydantic validation will fail
            with pytest.raises(pydantic.ValidationError):
                await PydanticNoFetch.from_hare_orm(instance)

        # After refreshing from db, pydantic model succeeds
        refreshed = await NoFetchDefaultModel.objects.get(pk=instance.pk)
        pydantic_instance = await PydanticNoFetch.from_hare_orm(refreshed)
        assert pydantic_instance.int_val == 1
        assert pydantic_instance.char_val == "test"

    def test_db_default_only_field_not_required_in_schema(self):
        """A field with only a db_default (no plain ``default``, not null, not generated) is
        NOT required by ``Field.required`` - the ORM lets .create() omit it and has the DB fill
        it in. The generated pydantic schema must agree, or a create-payload validated against
        it would wrongly reject an omitted db_default field."""
        PydanticDefault = pydantic_model_creator(DefaultModel, name="DefaultModelSchemaTest")
        schema = PydanticDefault.model_json_schema()

        for field_name in (
            "int_default",
            "float_default",
            "decimal_default",
            "bool_default",
            "char_default",
            "date_default",
            "datetime_default",
        ):
            assert field_name not in schema.get("required", []), (
                f"{field_name} has only a db_default and must not be required"
            )
            assert PydanticDefault.model_fields[field_name].default is None

        # Every db_default-only field must validate cleanly when omitted - only the
        # auto-generated `id` primary key is unrelated to this fix and still needs a value here.
        PydanticDefault.model_validate({"id": 1})
