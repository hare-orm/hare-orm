"""The public ways to ask a model which connection its queries use, what type a field is and how
the model's table and fields are laid out - ``Model.get_connection()``/``QuerySet.get_connection()``,
``Field.relation_type``/``encrypted``/``enum_type`` and the documented ``Model._meta`` attributes."""

from __future__ import annotations

import os
import sys
import types

import pytest
import pytest_asyncio

from hare import fields
from hare.cli.plugins import CLIContext
from hare.contrib.pydantic.descriptions import ModelDescription
from hare.contrib.test import requires_features
from hare.core.constants import DEFAULT_CONNECTION_NAME
from hare.exceptions import UnSupportedError
from hare.fields import RelationType
from hare.inspectdb import SchemaInspector
from hare.models import Model
from hare.transactions.transactions import Transactions
from tests.contrib.request_query.models import Visit
from tests.fields.models_encrypted import EncryptedRecord
from tests.testmodels import Address, Currency, EnumFields, Event, Service, Tournament, VersionedDocument
from tests.utils.multi_database_context import MultiDatabaseTestContext

MODULE_NAME = "tests._public_model_api_models"


class ReadWriteSplitRouter:
    def db_for_read(self, model):
        return "read_replica"

    def db_for_write(self, model):
        return "write_primary"


@pytest_asyncio.fixture
async def read_write_split():
    class SplitWidget(Model):
        id = fields.IntField(primary_key=True)
        name = fields.TextField()

        class Meta:
            app = "public_model_api"

    module = types.ModuleType(MODULE_NAME)
    setattr(module, "SplitWidget", SplitWidget)  # noqa: B010
    sys.modules[MODULE_NAME] = module
    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    try:
        async with MultiDatabaseTestContext.open(
            db_url,
            ["write_primary", "read_replica"],
            apps={"public_model_api": {"models": [MODULE_NAME], "default_connection": "write_primary"}},
            routers=[ReadWriteSplitRouter],
        ) as ctx:
            yield ctx, SplitWidget
    finally:
        sys.modules.pop(MODULE_NAME, None)


def test_get_connection_is_the_default_connection_without_a_router(db):
    connection = Tournament.get_connection()
    assert connection.connection_name == Tournament._meta.default_connection
    assert Tournament.get_connection(for_write=True) is connection
    assert Tournament.objects.all().get_connection() is connection
    assert Tournament.objects.filter(name="x").get_connection(for_write=True) is connection


def test_get_connection_follows_the_router(read_write_split):
    ctx, SplitWidget = read_write_split
    assert SplitWidget.get_connection().connection_name == "read_replica"
    assert SplitWidget.get_connection(for_write=True).connection_name == "write_primary"
    assert SplitWidget.objects.all().get_connection().connection_name == "read_replica"
    assert SplitWidget.objects.all().get_connection(for_write=True).connection_name == "write_primary"


def test_queryset_get_connection_returns_the_connection_bound_with_using_db(read_write_split):
    ctx, SplitWidget = read_write_split
    write_db = ctx.connections.get("write_primary")
    queryset = SplitWidget.objects.all().using(write_db)
    assert queryset.get_connection() is write_db
    assert queryset.get_connection(for_write=True) is write_db


def test_queryset_get_connection_binds_nothing(read_write_split):
    ctx, SplitWidget = read_write_split
    queryset = SplitWidget.objects.all()
    queryset.get_connection(for_write=True)
    assert queryset._db is None


@pytest.mark.asyncio
@requires_features(supports_transactions=True)
async def test_get_connection_inside_a_transaction_runs_in_the_transaction(db):
    class Rollback(Exception):
        pass

    with pytest.raises(Rollback):
        async with Transactions.atomic(Tournament._meta.default_connection):
            await Tournament.objects.using(Tournament.get_connection(for_write=True)).create(name="Rolled back")
            await (
                Tournament.objects.all()
                .using(Tournament.objects.all().get_connection(for_write=True))
                .update(name="Changed")
            )
            raise Rollback
    assert not await Tournament.objects.filter(name__in=["Rolled back", "Changed"]).exists()


@pytest.mark.asyncio
@requires_features(supports_transactions=True)
async def test_get_connection_locks_where_the_database_locks(db):
    tournament = await Tournament.objects.create(name="Locked")
    connection = Tournament.get_connection(for_write=True)
    async with Transactions.atomic(connection.connection_name) as transaction:
        queryset = Tournament.objects.filter(pk=tournament.pk).using(transaction)
        if transaction.features.supports_select_for_update:
            queryset = queryset.select_for_update()
            assert "FOR UPDATE" in queryset.sql()
        fetched = await queryset.get()
    assert fetched.pk == tournament.pk


@pytest.mark.asyncio
@requires_features(supports_select_for_update=False)
async def test_select_for_update_without_database_support_raises_when_run(db):
    connection = Tournament.get_connection(for_write=True)
    queryset = Tournament.objects.all().using(connection).select_for_update()
    with pytest.raises(UnSupportedError, match="select_for_update"):
        await queryset


def test_relation_type_names_every_relation_type(db):
    fields_map = Event._meta.fields_map
    assert fields_map["tournament"].relation_type is RelationType.FOREIGN_KEY
    assert fields_map["participants"].relation_type is RelationType.MANY_TO_MANY
    assert fields_map["address"].relation_type is RelationType.BACKWARD_ONE_TO_ONE
    assert Tournament._meta.fields_map["events"].relation_type is RelationType.BACKWARD_FOREIGN_KEY
    assert Address._meta.fields_map["event"].relation_type is RelationType.ONE_TO_ONE
    assert VersionedDocument._meta.fields_map["revision_notes"].relation_type is RelationType.BACKWARD_FOREIGN_KEY


def test_relation_type_is_none_for_plain_values(db):
    fields_map = Event._meta.fields_map
    assert fields_map["name"].relation_type is None
    assert fields_map["event_id"].relation_type is None
    # The key column of a forward relation holds a plain value.
    assert fields_map["tournament_id"].relation_type is None


def test_relation_type_matches_the_relation_sets_of_meta(db):
    for model in (Event, Tournament, Address, VersionedDocument):
        meta = model._meta
        expected_by_type = {
            RelationType.FOREIGN_KEY: meta.fk_fields,
            RelationType.ONE_TO_ONE: meta.o2o_fields,
            RelationType.MANY_TO_MANY: meta.m2m_fields,
            RelationType.BACKWARD_FOREIGN_KEY: meta.backward_fk_fields,
            RelationType.BACKWARD_ONE_TO_ONE: meta.backward_o2o_fields,
        }
        for relation_type, names in expected_by_type.items():
            assert {name for name, field in meta.fields_map.items() if field.relation_type is relation_type} == names
        assert {name for name, field in meta.fields_map.items() if field.relation_type is not None} == (
            meta.fetch_fields
        )


def test_encrypted_marks_only_encrypted_fields():
    fields_map = EncryptedRecord._meta.fields_map
    assert fields_map["secret"].encrypted is True
    assert fields_map["config"].encrypted is True
    assert fields_map["public_note"].encrypted is True
    assert fields_map["title"].encrypted is False
    assert fields_map["api_token"].encrypted is False
    assert fields.EncryptedTextField().encrypted is True
    assert fields.TextField().encrypted is False


def test_enum_type_is_the_enum_of_an_enum_field():
    fields_map = EnumFields._meta.fields_map
    assert fields_map["service"].enum_type is Service
    assert fields_map["currency"].enum_type is Currency
    assert fields_map["id"].enum_type is None
    assert fields.CharField(max_length=10).enum_type is None


def test_meta_primary_key_names(db):
    assert Event._meta.pk_attr_names == ("event_id",)
    assert VersionedDocument._meta.pk_attr_names == ("id", "version")
    assert Visit._meta.pk_attr_names == ()
    assert VersionedDocument._meta.pk is None
    assert [field.model_field_name for field in VersionedDocument._meta.pk_fields] == ["id", "version"]


def test_meta_names_and_columns(db):
    meta = Event._meta
    assert meta.full_name == "models.Event"
    assert meta.db_table == "event"
    assert set(meta.fields_map) == meta.fields
    assert meta.fields_db_projection["tournament_id"] == "tournament_id"
    assert "tournament" not in meta.fields_db_projection
    assert set(meta.db_fields) == set(meta.fields_db_projection.values())


def test_model_description_groups_fields_by_type(db):
    description = ModelDescription.from_model(Event)
    assert [field.model_field_name for field in description.pk_fields] == ["event_id"]
    assert {field.model_field_name for field in description.data_fields} == {
        "name",
        "modified",
        "token",
        "alias",
        "reporter_id",
        "tournament_id",
    }
    assert {field.model_field_name for field in description.fk_fields} == {"tournament", "reporter"}
    assert [field.model_field_name for field in description.m2m_fields] == ["participants"]
    assert [field.model_field_name for field in description.backward_o2o_fields] == ["address"]
    assert description.o2o_fields == []
    assert description.backward_fk_fields == []


def test_cli_context_holds_the_global_options():
    context = CLIContext(config="settings.HARE_ORM")
    assert context.config == "settings.HARE_ORM"


def test_default_connection_name_is_the_single_url_connection():
    assert DEFAULT_CONNECTION_NAME == "default"


@pytest.mark.asyncio
async def test_schema_inspector_generates_a_module_for_a_table(db):
    connection = Tournament.get_connection()
    source = await SchemaInspector.inspect(connection, [Tournament._meta.db_table])
    assert "class Tournament(Model):" in source
    assert "from hare import fields" in source or "fields." in source
