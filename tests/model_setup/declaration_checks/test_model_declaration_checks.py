"""A model whose declaration can't work is refused when it is bound, with a message naming the
mistake - not by a raw error of the database or of the first query reaching it."""

import pytest

from hare import fields
from hare.ddl import Index, UniqueConstraint
from hare.exceptions import ConfigurationError
from hare.migrations.state.state_apps import StateApps
from hare.models import Model
from hare.query.expressions import Q

pytestmark = pytest.mark.database_independent


def bind(*models: type[Model]) -> None:
    apps = StateApps()
    for model in models:
        apps.register_model("models", model)
    apps.init_relations()


def test_a_unique_constraint_without_fields_is_refused():
    with pytest.raises(ConfigurationError, match="UniqueConstraint.fields names no field"):
        UniqueConstraint(fields=(), name="empty_unique")


def test_a_field_name_with_the_lookup_separator_is_refused():
    class SeparatedName(Model):
        id = fields.IntField(primary_key=True)
        a__b = fields.IntField()

    with pytest.raises(ConfigurationError, match="SeparatedName.a__b: a field name can't contain '__'"):
        bind(SeparatedName)


def test_a_field_name_ending_in_an_underscore_is_refused():
    """from___gt read as the field from and the lookup _gt - every lookup of such a field failed."""

    class TrailingName(Model):
        id = fields.IntField(primary_key=True)
        from_ = fields.IntField(source_field="from")

    with pytest.raises(ConfigurationError, match="TrailingName.from_: a field name can't end in '_'"):
        bind(TrailingName)

    class TrailingRelationTarget(Model):
        id = fields.IntField(primary_key=True)

    class TrailingRelation(Model):
        id = fields.IntField(primary_key=True)
        target_ = fields.ForeignKeyField("models.TrailingRelationTarget")

    with pytest.raises(ConfigurationError, match="TrailingRelation.target_: a field name can't end in '_'"):
        bind(TrailingRelationTarget, TrailingRelation)


def test_a_related_name_with_the_lookup_separator_is_refused():
    class SeparatedTarget(Model):
        id = fields.IntField(primary_key=True)

    class SeparatedSource(Model):
        id = fields.IntField(primary_key=True)
        target = fields.ForeignKeyField("models.SeparatedTarget", related_name="source__rows")

    with pytest.raises(ConfigurationError, match="SeparatedTarget.source__rows"):
        bind(SeparatedTarget, SeparatedSource)


def test_get_latest_by_naming_an_unknown_field_is_refused():
    class LatestByMissing(Model):
        id = fields.IntField(primary_key=True)
        created = fields.DatetimeField()

        class Meta:
            get_latest_by = ("-created", "missing")

    with pytest.raises(ConfigurationError, match="get_latest_by of LatestByMissing names unknown fields missing"):
        bind(LatestByMissing)


def test_get_latest_by_takes_a_relation_path_and_pk():
    class LatestTarget(Model):
        id = fields.IntField(primary_key=True)
        name = fields.CharField(max_length=10)

    class LatestSource(Model):
        id = fields.IntField(primary_key=True)
        target = fields.ForeignKeyField("models.LatestTarget")

        class Meta:
            get_latest_by = ("target__name", "-pk")

    bind(LatestTarget, LatestSource)
    assert LatestSource._meta.get_latest_by == ("target__name", "-pk")


def test_a_self_relation_in_a_composite_primary_key_is_refused():
    class SelfKeyed(Model):
        number = fields.IntField()
        parent = fields.ForeignKeyField("models.SelfKeyed")
        pk = fields.CompositePrimaryKey("number", "parent")

    with pytest.raises(ConfigurationError, match="can't be a component of its composite primary key"):
        bind(SelfKeyed)


def test_a_table_name_holding_a_dot_is_refused():
    """The automatic through table named after it read as an "app.Model" reference in migrations."""
    with pytest.raises(ConfigurationError, match="holds a dot - a table's schema is named by Meta.schema"):

        class Dotted(Model):
            id = fields.IntField(primary_key=True)

            class Meta:
                abstract = True
                table = "my.table"


@pytest.mark.parametrize(
    "key_field",
    [
        fields.ArrayField(fields.IntField()),
        fields.JSONField(),
        fields.TupleField([fields.IntField(), fields.IntField()]),
        fields.MapField(fields.CharField(max_length=5), fields.IntField()),
    ],
)
def test_a_container_value_in_the_primary_key_is_refused(key_field):
    class ContainerKey(Model):
        part = key_field
        number = fields.IntField()
        pk = fields.CompositePrimaryKey("part", "number")

    with pytest.raises(ConfigurationError, match="can't be part of the primary key"):
        bind(ContainerKey)


def test_a_unique_index_and_a_unique_constraint_of_different_conditions_are_both_kept():
    """A unique PartialIndex and a UniqueConstraint over the same fields were refused as one rule
    declared twice, whatever their conditions."""
    from hare.ddl import PartialIndex
    from hare.query.expressions import Q

    def declare(index_condition: Q, constraint_condition: Q) -> type[Model]:
        class Ticket(Model):
            id = fields.IntField(primary_key=True)
            a = fields.IntField()
            b = fields.CharField(max_length=10)

            class Meta:
                indexes = [PartialIndex(fields=["b"], condition=index_condition, unique=True, name="ticket_b_part")]
                constraints = [UniqueConstraint(fields=["b"], condition=constraint_condition, name="ticket_b_cond")]

        return Ticket

    bind(declare(Q(a__gt=5), Q(a__gt=100)))
    with pytest.raises(ConfigurationError, match="covers the exact same fields"):
        bind(declare(Q(a__gt=5), Q(a__gt=5)))


def test_an_instead_of_trigger_of_a_table_is_refused():
    """It was taken, and migrate failed with the database's error - an INSTEAD OF trigger fires on
    a view only."""
    from hare.ddl import RawSQLTerm, Trigger
    from hare.ddl.enums import TriggerEvent, TriggerTiming

    class Ledger(Model):
        id = fields.IntField(primary_key=True)

        class Meta:
            triggers = [
                Trigger(
                    name="ledger_redirect",
                    on=TriggerEvent.INSERT,
                    body=RawSQLTerm("RETURN NEW;"),
                    timing=TriggerTiming.INSTEAD_OF,
                )
            ]

    with pytest.raises(ConfigurationError, match="fires INSTEAD OF - the database fires such a trigger on a view"):
        bind(Ledger)


def test_a_schema_text_holding_a_null_byte_is_refused():
    """Taken until a migration wrote it - on ClickHouse with part of the migration applied."""
    for declare in (
        lambda: fields.CharField(max_length=10, description="a\x00b"),
        lambda: fields.CharField(max_length=10, default="a\x00b"),
        lambda: fields.CharField(max_length=10, db_default="a\x00b"),
    ):
        with pytest.raises(ConfigurationError, match="can't hold a null byte"):
            declare()
    with pytest.raises(ConfigurationError, match="table_description is written into the schema"):

        class Described(Model):
            class Meta:
                table_description = "a\x00b"

    fields.CharField(max_length=10, default=lambda: "a\x00b", description="plain")


def test_a_name_the_schema_cant_hold_is_refused():
    """An empty column or table name, or one with a null byte, reached the database - a raw error there."""

    class NulColumn(Model):
        id = fields.IntField(primary_key=True)
        name = fields.CharField(max_length=10, source_field="a\x00b")

    with pytest.raises(ConfigurationError, match="isn't a name the schema can hold"):
        bind(NulColumn)

    class EmptyTable(Model):
        id = fields.IntField(primary_key=True)

        class Meta:
            table = "t\x00"

    with pytest.raises(ConfigurationError, match="isn't a name the schema can hold"):
        bind(EmptyTable)


def test_an_operation_naming_a_model_by_no_name_is_refused():
    """RenameModel to a name with a null byte failed with Python's own ValueError of type()."""
    from hare.migrations import operations as ops

    for build in (
        lambda: ops.RenameModel("Item", "a\x00b"),
        lambda: ops.RenameModel("Item", ""),
        lambda: ops.CreateModel(name="", fields=[("id", fields.IntField(primary_key=True))]),
    ):
        with pytest.raises(ConfigurationError, match="can't name a model"):
            build()


@pytest.mark.parametrize("declaration", ["index", "unique_constraint"])
def test_an_include_naming_no_column_is_refused_and_a_relation_is_taken(declaration):
    """An index's include wasn't checked - the database refused the missing column after the SQL ran, or
    SQLite and ClickHouse kept the index without it."""

    def make_entry(include: tuple[str, ...]) -> Index | UniqueConstraint:
        if declaration == "index":
            return Index(fields=("code",), include=include)
        return UniqueConstraint(fields=("code",), include=include, name="uid_code")

    class IncludedTarget(Model):
        id = fields.IntField(primary_key=True)

    class IncludingRelation(Model):
        id = fields.IntField(primary_key=True)
        code = fields.IntField()
        target = fields.ForeignKeyField("models.IncludedTarget")

        class Meta:
            indexes = (make_entry(("target",)),) if declaration == "index" else ()
            constraints = (make_entry(("target",)),) if declaration != "index" else ()

    bind(IncludedTarget, IncludingRelation)

    class IncludingMissing(Model):
        id = fields.IntField(primary_key=True)
        code = fields.IntField()

        class Meta:
            indexes = (make_entry(("nope",)),) if declaration == "index" else ()
            constraints = (make_entry(("nope",)),) if declaration != "index" else ()

    with pytest.raises(ConfigurationError, match="include 'nope' is not a field stored on IncludingMissing"):
        bind(IncludingMissing)


def test_a_unique_constraint_condition_reading_no_column_is_refused():
    """ClickHouse took it - hare checks the uniqueness there, no SQL of the condition refused it."""

    class ConditionalUnique(Model):
        id = fields.IntField(primary_key=True)
        code = fields.IntField()

        class Meta:
            constraints = (UniqueConstraint(fields=("code",), condition=Q(nope=1), name="uid_code_when"),)

    with pytest.raises(ConfigurationError, match="ConditionalUnique: a constraint or index condition reads only"):
        bind(ConditionalUnique)
