import pytest

from hare.contrib.test import requires_features
from hare.ddl.constraints import UniqueConstraint
from hare.ddl.indexes import Index
from hare.exceptions import ConfigurationError, IntegrityError
from tests.testmodels import (
    CompositePkThing,
    JSONFields,
    Tournament,
    UniqueTogetherFields,
    UniqueTogetherFieldsWithFK,
)


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_unique_together(db):
    first_name = "first_name"
    last_name = "last_name"

    await UniqueTogetherFields.objects.create(first_name=first_name, last_name=last_name)

    with pytest.raises(IntegrityError):
        await UniqueTogetherFields.objects.create(first_name=first_name, last_name=last_name)


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_unique_together_with_foreign_keys(db):
    tournament_name = "tournament_name"
    text = "text"

    tournament = await Tournament.objects.create(name=tournament_name)

    await UniqueTogetherFieldsWithFK.objects.create(text=text, tournament=tournament)

    with pytest.raises(IntegrityError):
        await UniqueTogetherFieldsWithFK.objects.create(text=text, tournament=tournament)


def test_valid_unique_constraints_and_indexes_pass_indexability_check(db):
    """Sanity anchor - an already-valid UniqueConstraint doesn't get flagged."""
    UniqueTogetherFields._meta._validate_together_field_indexability()


def test_unique_constraint_on_non_indexable_field_raises(db):
    """JSONFields.data is a JSONField (indexable = False) - a UniqueConstraint on it raises at
    model-registration time, not as an opaque raw DB error only once DDL runs."""
    meta = JSONFields._meta
    original = meta.constraints
    meta.constraints = (UniqueConstraint(fields=("data", "id")),)
    try:
        with pytest.raises(ConfigurationError, match="can't be indexed"):
            meta._validate_together_field_indexability()
    finally:
        meta.constraints = original


def test_unique_constraint_unknown_field_raises(db):
    meta = CompositePkThing._meta
    original = meta.constraints
    meta.constraints = (UniqueConstraint(fields=("thing_id", "not_a_real_field")),)
    try:
        with pytest.raises(ConfigurationError, match="not_a_real_field"):
            meta._validate_together_field_indexability()
    finally:
        meta.constraints = original


def test_tuple_form_indexes_on_non_indexable_field_raises(db):
    meta = JSONFields._meta
    original = meta.indexes
    meta.indexes = (("data",),)
    try:
        with pytest.raises(ConfigurationError, match="indexes"):
            meta._validate_together_field_indexability()
    finally:
        meta.indexes = original


def test_index_object_form_indexes_on_non_indexable_field_raises(db):
    meta = JSONFields._meta
    original = meta.indexes
    meta.indexes = (Index(fields=("data",)),)
    try:
        with pytest.raises(ConfigurationError, match="indexes"):
            meta._validate_together_field_indexability()
    finally:
        meta.indexes = original


def build_models_with_source_field_foreign_key():
    from hare import Hare, fields
    from hare.core.connections import Connections
    from hare.models import Model

    class UtSourceOwner(Model):
        id = fields.IntField(primary_key=True)

        class Meta:
            table = "ut_source_owner"

    class UtSourceMembership(Model):
        id = fields.IntField(primary_key=True)
        owner = fields.ForeignKeyField("ut_source.UtSourceOwner", source_field="owner_ref", related_name="memberships")
        role = fields.CharField(max_length=10)

        class Meta:
            table = "ut_source_membership"
            constraints = (UniqueConstraint(fields=("owner", "role")),)
            indexes = [("owner", "role")]

    alias = next(iter(Connections.current().db_config))
    Hare.register_live_models([UtSourceOwner, UtSourceMembership], app_label="ut_source", connection_alias=alias)
    return UtSourceOwner, UtSourceMembership


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("ddl_path", ["generate_schemas", "migrations"])
async def test_unique_together_and_index_over_foreign_key_use_its_source_field_column(db, ddl_path):
    """unique_together/indexes naming an FK with source_field= used to reference "<attr>_id"
    instead of the real key column, producing DDL that failed outright."""
    from hare import Hare
    from hare.core.connections import Connections
    from hare.inspectdb import SchemaIntrospector

    owner_class, membership_class = build_models_with_source_field_foreign_key()
    connection = Connections.get(next(iter(Connections.current().db_config)))
    # One IntegrityError per test (Postgres aborts the surrounding test transaction on it), so
    # leftovers are dropped up front rather than afterwards.
    await connection.execute_script("DROP TABLE IF EXISTS ut_source_membership")
    await connection.execute_script("DROP TABLE IF EXISTS ut_source_owner")
    try:
        assert membership_class._meta.get_column_names(["owner", "role", "owner_id"]) == [
            "owner_ref",
            "role",
            "owner_ref",
        ]
        if ddl_path == "generate_schemas":
            schema_generator = connection.dialect.schema_editor_class(connection)
            for model in (owner_class, membership_class):
                table_sql = schema_generator._get_model_sql_data(model).get_table_creation_sql()
                assert "owner_id" not in table_sql
                await connection.execute_script(table_sql)
        else:
            editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
            await editor.create_model(owner_class)
            await editor.create_model(membership_class)

        table_info = await SchemaIntrospector.inspect_table(connection, "ut_source_membership")
        assert sorted((tuple(index.columns), index.is_unique) for index in table_info.indexes) == [
            (("owner_ref", "role"), False),
            (("owner_ref", "role"), True),
        ]
        owner = await owner_class.objects.create(id=1)
        await membership_class.objects.create(id=1, owner=owner, role="a")
        with pytest.raises(IntegrityError):
            await membership_class.objects.create(id=2, owner=owner, role="a")
    finally:
        Hare.unregister_live_models([membership_class])
        Hare.unregister_live_models([owner_class])
