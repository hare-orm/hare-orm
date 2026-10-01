"""Hare.register_live_model(managed=...) / Hare.unregister_live_model() and
hare.inspectdb.ModelFactory - building, registering, unregistering and re-registering runtime
models."""

from typing import Any

import pytest

from hare import Hare, fields
from hare.contrib.test import requires_features
from hare.core.connections import Connections
from hare.exceptions import ConfigurationError, FieldError
from hare.fields.relations.fields import ForeignKeyFieldInstance
from hare.inspectdb import (
    ManyToManyThroughTableSkippedError,
    ModelFactory,
    ModelSourceGenerator,
    SchemaIntrospector,
)
from hare.migrations.writer import ImportManager, MigrationWriter
from hare.models import Model
from hare.models.deletion.deletion_graph import DeletionGraph


@pytest.fixture
def connection(db):
    alias = next(iter(Connections.current().db_config))
    return Connections.get(alias)


def build_parent_and_child_classes(parent_name: str = "LiveParent", child_name: str = "LiveChild"):
    parent_class = type(
        parent_name,
        (Model,),
        {
            "__module__": __name__,
            "id": fields.IntField(primary_key=True),
            "name": fields.CharField(max_length=32),
            "Meta": type("Meta", (), {"table": "live_parent"}),
        },
    )
    child_class = type(
        child_name,
        (Model,),
        {
            "__module__": __name__,
            "id": fields.IntField(primary_key=True),
            "parent": fields.ForeignKeyField(f"live.{parent_name}", related_name="children"),
            "Meta": type("Meta", (), {"table": "live_child"}),
        },
    )
    return parent_class, child_class


async def create_table(connection, table_name: str, create_sql: str) -> None:
    """SQLite's executescript() commits the surrounding test transaction, so a table can outlive
    the test that created it there - dropped first to always start from an empty table."""
    await connection.execute_script(f"DROP TABLE IF EXISTS {table_name}")
    await connection.execute_script(create_sql)


async def drop_tables(connection, *table_names: str) -> None:
    for table_name in table_names:
        await connection.execute_script(f"DROP TABLE IF EXISTS {table_name}")


async def create_parent_and_child_tables(connection) -> None:
    await drop_tables(connection, "live_child", "live_parent")
    await create_table(
        connection, "live_parent", "CREATE TABLE live_parent (id INTEGER PRIMARY KEY, name VARCHAR(32) NOT NULL)"
    )
    await create_table(
        connection,
        "live_child",
        "CREATE TABLE live_child (id INTEGER PRIMARY KEY, parent_id INTEGER NOT NULL REFERENCES live_parent (id))",
    )


@pytest.mark.asyncio
async def test_register_unregister_register_same_name_queries_work(db, connection):
    alias = next(iter(Connections.current().db_config))
    await create_parent_and_child_tables(connection)
    first_parent, _ = build_parent_and_child_classes()
    Hare.register_live_models([first_parent], app_label="live", connection_alias=alias)
    await first_parent.objects.create(id=1, name="first")

    Hare.unregister_live_models([first_parent])

    assert "live" not in Hare.apps
    assert first_parent._meta.default_connection is None
    with pytest.raises(ConfigurationError):
        await first_parent.objects.all()

    second_parent, _ = build_parent_and_child_classes()
    Hare.register_live_models([second_parent], app_label="live", connection_alias=alias)
    try:
        assert Hare.apps["live"]["LiveParent"] is second_parent
        await second_parent.objects.create(id=2, name="second")
        assert sorted(parent.name for parent in await second_parent.objects.all()) == ["first", "second"]
    finally:
        Hare.unregister_live_models([second_parent])


@pytest.mark.asyncio
async def test_unregister_removes_backward_relations_from_fk_target(db, connection):
    alias = next(iter(Connections.current().db_config))
    await create_parent_and_child_tables(connection)
    parent_class, child_class = build_parent_and_child_classes()
    Hare.register_live_models([parent_class], app_label="live", connection_alias=alias)
    Hare.register_live_models([child_class], app_label="live", connection_alias=alias)
    parent = await parent_class.objects.create(id=1, name="p")
    await child_class.objects.create(id=1, parent=parent)
    assert [child.id for child in await parent.children] == [1]
    assert await parent_class.objects.filter(children__id=1).count() == 1
    assert DeletionGraph.get_backward_relations(parent_class)

    Hare.unregister_live_models([child_class])

    try:
        assert "children" not in parent_class._meta.fields_map
        assert "children" not in parent_class._meta.backward_fk_fields
        assert "children" not in parent_class._meta.fetch_fields
        with pytest.raises(FieldError):
            parent_class._meta.get_lookup_info("children__isnull")
        assert "children" not in parent_class.__dict__
        assert DeletionGraph.get_backward_relations(parent_class) == []
        assert "LiveChild" not in Hare.apps["live"]
        with pytest.raises(Exception, match="children"):
            await parent_class.objects.filter(children__id=1).count()
        # The target itself keeps working, deletes included (no stale cascade relation).
        fetched = await parent_class.objects.get(id=1)
        assert fetched.name == "p"
        await connection.execute_script("DELETE FROM live_child")
        await fetched.delete()
        assert await parent_class.objects.all().count() == 0
    finally:
        Hare.unregister_live_models([parent_class])


@pytest.mark.asyncio
async def test_unregister_target_still_referenced_raises_and_changes_nothing(db, connection):
    alias = next(iter(Connections.current().db_config))
    parent_class, child_class = build_parent_and_child_classes()
    Hare.register_live_models([parent_class], app_label="live", connection_alias=alias)
    Hare.register_live_models([child_class], app_label="live", connection_alias=alias)
    try:
        with pytest.raises(ConfigurationError, match=r"still referenced by live\.LiveChild\.parent"):
            Hare.unregister_live_models([parent_class])
        assert Hare.apps["live"]["LiveParent"] is parent_class
        assert "children" in parent_class._meta.fields_map
        assert parent_class._meta.default_connection == alias
    finally:
        Hare.unregister_live_models([child_class])
        Hare.unregister_live_models([parent_class])


@pytest.mark.asyncio
async def test_same_class_can_be_registered_again_after_unregister(db, connection):
    """The class's own relation-init state (the FK's shadow column, the target's backward
    relation) is reset by unregister, so registering the SAME class object again doesn't trip
    over "Field parent_id already present" or a duplicate backward relation."""
    alias = next(iter(Connections.current().db_config))
    await create_parent_and_child_tables(connection)
    parent_class, child_class = build_parent_and_child_classes()
    Hare.register_live_models([parent_class], app_label="live", connection_alias=alias)
    Hare.register_live_models([child_class], app_label="live", connection_alias=alias)
    Hare.unregister_live_models([child_class])
    assert "parent_id" not in child_class._meta.fields_map

    Hare.register_live_models([child_class], app_label="live", connection_alias=alias)
    try:
        parent = await parent_class.objects.create(id=1, name="p")
        await child_class.objects.create(id=5, parent=parent)
        child = await child_class.objects.get(id=5).select_related("parent")
        assert child.parent.name == "p"
        assert [child.id for child in await parent.children] == [5]
    finally:
        Hare.unregister_live_models([child_class])
        Hare.unregister_live_models([parent_class])


@pytest.mark.asyncio
async def test_unregister_self_referential_model_and_register_again(db, connection):
    alias = next(iter(Connections.current().db_config))
    await create_table(
        connection,
        "live_node",
        "CREATE TABLE live_node (id INTEGER PRIMARY KEY, parent_id INTEGER NULL REFERENCES live_node (id))",
    )

    def build_node_class() -> type[Model]:
        return type(
            "LiveNode",
            (Model,),
            {
                "__module__": __name__,
                "id": fields.IntField(primary_key=True),
                "parent": fields.ForeignKeyField("live.LiveNode", related_name="kids", null=True),
                "Meta": type("Meta", (), {"table": "live_node"}),
            },
        )

    first_node_class = build_node_class()
    Hare.register_live_models([first_node_class], app_label="live", connection_alias=alias)
    Hare.unregister_live_models([first_node_class])
    assert "kids" not in first_node_class._meta.fields_map

    second_node_class = build_node_class()
    Hare.register_live_models([second_node_class], app_label="live", connection_alias=alias)
    try:
        root = await second_node_class.objects.create(id=1)
        await second_node_class.objects.create(id=2, parent=root)
        assert [node.id for node in await root.kids] == [2]
    finally:
        Hare.unregister_live_models([second_node_class])


@pytest.mark.asyncio
async def test_unregister_removes_generated_m2m_field_from_target(db, connection):
    alias = next(iter(Connections.current().db_config))
    tag_class = type(
        "LiveTag",
        (Model,),
        {
            "__module__": __name__,
            "id": fields.IntField(primary_key=True),
            "Meta": type("Meta", (), {"table": "live_tag"}),
        },
    )
    post_class = type(
        "LivePost",
        (Model,),
        {
            "__module__": __name__,
            "id": fields.IntField(primary_key=True),
            "tags": fields.ManyToManyField("live.LiveTag", related_name="posts", through="live_post_tag"),
            "Meta": type("Meta", (), {"table": "live_post"}),
        },
    )
    Hare.register_live_models([tag_class], app_label="live", connection_alias=alias)
    Hare.register_live_models([post_class], app_label="live", connection_alias=alias)
    assert "posts" in tag_class._meta.m2m_fields

    Hare.unregister_live_models([post_class])
    try:
        assert "posts" not in tag_class._meta.fields_map
        assert "posts" not in tag_class._meta.m2m_fields
        assert "posts" not in tag_class.__dict__
    finally:
        Hare.unregister_live_models([tag_class])


@pytest.mark.asyncio
async def test_unregister_model_that_is_not_registered_raises(db):
    class NeverRegistered(Model):
        id = fields.IntField(primary_key=True)

    with pytest.raises(ConfigurationError, match="is not registered"):
        Hare.unregister_live_models([NeverRegistered])


@pytest.mark.asyncio
async def test_pydantic_model_of_reregistered_class_points_at_the_new_class(db, connection):
    """A structurally identical class re-registered for the same table used to get the old class's
    cached Pydantic model (same schema/table/field hash), whose orig_model still pointed at the
    unregistered class."""
    from hare.contrib.pydantic import pydantic_model_creator

    alias = next(iter(Connections.current().db_config))
    first_parent, _ = build_parent_and_child_classes()
    Hare.register_live_models([first_parent], app_label="live", connection_alias=alias)
    first_schema = pydantic_model_creator(first_parent)
    assert first_schema.model_config["orig_model"] is first_parent
    Hare.unregister_live_models([first_parent])

    second_parent, _ = build_parent_and_child_classes()
    Hare.register_live_models([second_parent], app_label="live", connection_alias=alias)
    try:
        assert pydantic_model_creator(second_parent).model_config["orig_model"] is second_parent
    finally:
        Hare.unregister_live_models([second_parent])


# ============================================================================
# ModelFactory.from_table_info vs ModelSourceGenerator
# ============================================================================


def render_deconstructed(value: Any) -> str:
    path, args, kwargs = value.deconstruct()
    return MigrationWriter.render_call(path, list(args), dict(kwargs), ImportManager())


def describe_model_class(model: type[Model]) -> dict[str, Any]:
    meta = model._meta
    return {
        "name": model.__name__,
        "fields": {name: render_deconstructed(field) for name, field in meta.fields_map.items()},
        "field_classes": {name: type(field) for name, field in meta.fields_map.items()},
        "pk_attr": meta.pk_attr,
        "db_table": meta.db_table,
        "schema": meta.schema,
        "indexes": [render_deconstructed(index) if hasattr(index, "deconstruct") else index for index in meta.indexes],
        "constraints": [render_deconstructed(constraint) for constraint in meta.constraints],
        "table_description": meta.table_description,
    }


def exec_generated_source(source: str, class_name: str) -> type[Model]:
    namespace: dict[str, Any] = {"__name__": "tests.generated_live_module"}
    exec(compile(source, "<generated>", "exec"), namespace)  # noqa: S102 - test-only, own generated code
    return namespace[class_name]


async def assert_factory_matches_generator(connection, table_name: str, class_name: str, **options: Any):
    dialect = connection.dialect.name
    table_info = await SchemaIntrospector.inspect_table(connection, table_name)
    factory_class = ModelFactory.from_table_info(table_info, dialect, app_label="live", **options)
    source = ModelSourceGenerator.generate_model_source(table_info, dialect, app_label="live", **options)
    generated_class = exec_generated_source(source, class_name)
    factory_description = describe_model_class(factory_class)
    assert factory_description == describe_model_class(generated_class)
    return factory_class, factory_description


async def create_equivalence_tables(connection, dialect: str) -> None:
    json_type = "JSONB" if dialect == "postgresql" else "JSON"
    array_column = ", tags INTEGER[] NOT NULL, labels VARCHAR(20)[] NULL" if dialect == "postgresql" else ""
    await drop_tables(connection, "live_eq_line", "live_eq_parent")
    await create_table(
        connection,
        "live_eq_parent",
        "CREATE TABLE live_eq_parent (id INTEGER PRIMARY KEY, name VARCHAR(50) NOT NULL UNIQUE, "
        f"payload {json_type} NULL, price NUMERIC(10, 2) NOT NULL{array_column})",
    )
    await create_table(
        connection,
        "live_eq_line",
        "CREATE TABLE live_eq_line (order_no INTEGER NOT NULL, line_no INTEGER NOT NULL, "
        "parent_id INTEGER NULL REFERENCES live_eq_parent (id) ON DELETE SET NULL, "
        "note VARCHAR(40) NULL, PRIMARY KEY (order_no, line_no))",
    )
    await connection.execute_script("CREATE INDEX live_eq_line_note_idx ON live_eq_line (note, line_no)")


@pytest.mark.asyncio
async def test_factory_and_generator_build_equivalent_models(db, connection):
    dialect = connection.dialect.name
    await create_equivalence_tables(connection, dialect)

    parent_class, parent_description = await assert_factory_matches_generator(
        connection, "live_eq_parent", "LiveEqParent"
    )
    line_class, line_description = await assert_factory_matches_generator(connection, "live_eq_line", "LiveEqLine")

    assert parent_description["field_classes"]["payload"] is fields.JSONField
    assert parent_description["field_classes"]["price"] is fields.DecimalField
    assert line_description["pk_attr"] == ("order_no", "line_no")
    assert line_description["field_classes"]["parent"] is ForeignKeyFieldInstance
    assert "fields.ForeignKeyField('live.LiveEqParent'" in line_description["fields"]["parent"]
    assert "on_delete=OnDelete.SET_NULL" in line_description["fields"]["parent"]
    if dialect == "postgresql":
        from hare.dialects.postgresql.fields.array import ArrayField

        assert parent_description["field_classes"]["tags"] is ArrayField
        assert parent_class._meta.fields_map["tags"].base_field.__class__ is fields.IntField
        assert parent_class._meta.fields_map["labels"].base_field.max_length == 20

    alias = next(iter(Connections.current().db_config))
    Hare.register_live_models([parent_class], app_label="live", connection_alias=alias, managed=False)
    Hare.register_live_models([line_class], app_label="live", connection_alias=alias, managed=False)
    try:
        extra: dict[str, Any] = {"tags": [1, 2], "labels": ["a"]} if dialect == "postgresql" else {}
        parent = await parent_class.objects.create(id=1, name="p", payload={"k": [1]}, price="9.50", **extra)
        await line_class.objects.create(order_no=1, line_no=2, parent=parent, note="n")
        line = await line_class.objects.get(order_no=1, line_no=2).select_related("parent")
        assert line.parent.payload == {"k": [1]}
        if dialect == "postgresql":
            assert line.parent.tags == [1, 2]
        assert line_class._meta.managed is False
    finally:
        Hare.unregister_live_models([line_class])
        Hare.unregister_live_models([parent_class])


@pytest.mark.asyncio
async def test_factory_honours_fk_target_overrides(db, connection):
    await create_equivalence_tables(connection, connection.dialect.name)
    await assert_factory_matches_generator(
        connection, "live_eq_line", "LiveEqLine", fk_target_overrides={"live_eq_parent": "models.RealParent"}
    )
    table_info = await SchemaIntrospector.inspect_table(connection, "live_eq_line")
    line_class = ModelFactory.from_table_info(
        table_info,
        connection.dialect.name,
        app_label="live",
        fk_target_overrides={"live_eq_parent": "models.RealParent"},
    )
    assert line_class._meta.fields_map["parent"].model_name == "models.RealParent"


@pytest.mark.asyncio
async def test_factory_m2m_through_table_skip_and_build(db, connection):
    await drop_tables(connection, "live_m2m_a_b", "live_m2m_a", "live_m2m_b")
    await create_table(connection, "live_m2m_a", "CREATE TABLE live_m2m_a (id INTEGER PRIMARY KEY)")
    await create_table(connection, "live_m2m_b", "CREATE TABLE live_m2m_b (id INTEGER PRIMARY KEY)")
    await create_table(
        connection,
        "live_m2m_a_b",
        "CREATE TABLE live_m2m_a_b (a_id INTEGER NOT NULL REFERENCES live_m2m_a (id), "
        "b_id INTEGER NOT NULL REFERENCES live_m2m_b (id), PRIMARY KEY (a_id, b_id))",
    )
    table_info = await SchemaIntrospector.inspect_table(connection, "live_m2m_a_b")
    dialect = connection.dialect.name

    with pytest.raises(ManyToManyThroughTableSkippedError, match="live_m2m_a_b"):
        ModelFactory.from_table_info(table_info, dialect, app_label="live")

    await assert_factory_matches_generator(connection, "live_m2m_a_b", "LiveM2mAB", skip_m2m_through_tables=False)


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_factory_generated_column_matches_generator(db, connection):
    await create_table(
        connection,
        "live_generated",
        "CREATE TABLE live_generated (id INTEGER PRIMARY KEY, a INTEGER NOT NULL, "
        "doubled INTEGER GENERATED ALWAYS AS (a * 2) STORED)",
    )
    _, description = await assert_factory_matches_generator(connection, "live_generated", "LiveGenerated")
    assert "GeneratedField" in description["fields"]["doubled"]


def build_child_class_with_related_names(fk_related_name: str, m2m_related_name: str, name: str = "LiveChild"):
    return type(
        name,
        (Model,),
        {
            "__module__": __name__,
            "id": fields.IntField(primary_key=True),
            "parent": fields.ForeignKeyField("live.LiveParent", related_name=fk_related_name),
            "tags": fields.ManyToManyField("live.LiveParent", related_name=m2m_related_name, through="live_child_tag"),
            "Meta": type("Meta", (), {"table": f"{name.lower()}_table"}),
        },
    )


@pytest.mark.asyncio
async def test_failed_registration_rolls_back_registry_and_backward_relations(db, connection):
    alias = next(iter(Connections.current().db_config))
    await create_parent_and_child_tables(connection)
    await create_table(
        connection,
        "livechild_table",
        "CREATE TABLE livechild_table (id INTEGER PRIMARY KEY, "
        "parent_id INTEGER NOT NULL REFERENCES live_parent (id))",
    )
    parent_class, _ = build_parent_and_child_classes()
    Hare.register_live_models([parent_class], app_label="live", connection_alias=alias)
    parent_fields_before = set(parent_class._meta.fields_map)
    # "name" collides with LiveParent.name - fails after the FK already added "children".
    bad_child = build_child_class_with_related_names("children", "name")
    try:
        with pytest.raises(ConfigurationError, match="duplicates"):
            Hare.register_live_models([bad_child], app_label="live", connection_alias=alias, managed=False)

        assert set(Hare.apps["live"]) == {"LiveParent"}
        assert set(parent_class._meta.fields_map) == parent_fields_before
        assert bad_child._meta.managed is True
        assert bad_child._meta.default_connection is None
        with pytest.raises(ConfigurationError, match="not registered"):
            Hare.unregister_live_models([bad_child])

        # An unrelated registration no longer trips over the half-registered model.
        unrelated = type(
            "LiveUnrelated",
            (Model,),
            {
                "__module__": __name__,
                "id": fields.IntField(primary_key=True),
                "Meta": type("Meta", (), {"table": "live_unrelated"}),
            },
        )
        Hare.register_live_models([unrelated], app_label="live", connection_alias=alias)
        Hare.unregister_live_models([unrelated])

        good_child = build_child_class_with_related_names("children", "tagged_children")
        Hare.register_live_models([good_child], app_label="live", connection_alias=alias)
        try:
            parent = await parent_class.objects.create(id=1, name="p")
            await good_child.objects.create(id=1, parent=parent)
            assert [child.id for child in await parent.children] == [1]
        finally:
            Hare.unregister_live_models([good_child])
    finally:
        Hare.unregister_live_models([parent_class])
        await drop_tables(connection, "livechild_table")


def build_cycle_classes():
    cycle_a = type(
        "LiveCycleA",
        (Model,),
        {
            "__module__": __name__,
            "id": fields.IntField(primary_key=True),
            "b": fields.ForeignKeyField("live.LiveCycleB", related_name="a_items", null=True, db_constraint=False),
            "Meta": type("Meta", (), {"table": "live_cycle_a"}),
        },
    )
    cycle_b = type(
        "LiveCycleB",
        (Model,),
        {
            "__module__": __name__,
            "id": fields.IntField(primary_key=True),
            "a": fields.ForeignKeyField("live.LiveCycleA", related_name="b_items", null=True, db_constraint=False),
            "Meta": type("Meta", (), {"table": "live_cycle_b"}),
        },
    )
    return cycle_a, cycle_b


@pytest.mark.asyncio
async def test_mutually_referencing_models_register_together(db, connection):
    alias = next(iter(Connections.current().db_config))
    await drop_tables(connection, "live_cycle_a", "live_cycle_b")
    await create_table(connection, "live_cycle_a", "CREATE TABLE live_cycle_a (id INTEGER PRIMARY KEY, b_id INTEGER)")
    await create_table(connection, "live_cycle_b", "CREATE TABLE live_cycle_b (id INTEGER PRIMARY KEY, a_id INTEGER)")
    cycle_a, cycle_b = build_cycle_classes()
    with pytest.raises(ConfigurationError, match="LiveCycleB"):
        Hare.register_live_models([cycle_a], app_label="live", connection_alias=alias)
    assert "live" not in Hare.apps
    assert cycle_a._meta._inited is False

    Hare.register_live_models([cycle_a, cycle_b], app_label="live", connection_alias=alias)
    try:
        assert cycle_a._meta.fields_map["b"].related_model is cycle_b
        assert cycle_b._meta.fields_map["a"].related_model is cycle_a
        assert "a_items" in cycle_b._meta.fields_map
        assert "b_items" in cycle_a._meta.fields_map
        first = await cycle_a.objects.create(id=1)
        second = await cycle_b.objects.create(id=1, a=first)
        first.b = second
        await first.save()
        loaded = await cycle_a.objects.all().select_related("b")
        assert [row.b.id for row in loaded] == [1]
        with pytest.raises(ConfigurationError, match="still referenced"):
            Hare.unregister_live_models([cycle_a])
    finally:
        Hare.unregister_live_models([cycle_a, cycle_b])
    assert "live" not in Hare.apps
    assert "a_items" not in cycle_b._meta.fields_map
    await drop_tables(connection, "live_cycle_a", "live_cycle_b")


@pytest.mark.asyncio
async def test_register_live_models_rolls_back_every_model_on_failure(db, connection):
    alias = next(iter(Connections.current().db_config))
    parent_class, _ = build_parent_and_child_classes()
    Hare.register_live_models([parent_class], app_label="live", connection_alias=alias)
    parent_fields_before = set(parent_class._meta.fields_map)
    good_child = build_child_class_with_related_names("children", "tagged_children", name="LiveGoodChild")
    bad_child = build_child_class_with_related_names("other_children", "name", name="LiveBadChild")
    try:
        with pytest.raises(ConfigurationError, match="duplicates"):
            Hare.register_live_models([good_child, bad_child], app_label="live", connection_alias=alias)
        assert set(Hare.apps["live"]) == {"LiveParent"}
        assert set(parent_class._meta.fields_map) == parent_fields_before
        assert good_child._meta._inited is False
    finally:
        Hare.unregister_live_models([parent_class])


@pytest.mark.asyncio
async def test_registering_a_different_class_under_a_taken_name_is_refused(db, connection):
    alias = next(iter(Connections.current().db_config))
    await create_parent_and_child_tables(connection)
    parent_class, first_child = build_parent_and_child_classes()
    _, second_child = build_parent_and_child_classes()
    Hare.register_live_models([parent_class], app_label="live", connection_alias=alias)
    Hare.register_live_models([first_child], app_label="live", connection_alias=alias)
    try:
        with pytest.raises(ConfigurationError, match="unregister_live_model"):
            Hare.register_live_models([second_child], app_label="live", connection_alias=alias)
        assert Hare.apps["live"]["LiveChild"] is first_child
        assert parent_class._meta.fields_map["children"].related_model is first_child
        # The same class again stays a no-op.
        Hare.register_live_models([first_child], app_label="live", connection_alias=alias)
        assert Hare.apps["live"]["LiveChild"] is first_child
    finally:
        Hare.unregister_live_models([first_child])
        Hare.unregister_live_models([parent_class])
