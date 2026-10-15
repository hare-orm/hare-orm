"""A model whose declaration can't work is refused when it is bound, with a message naming the
mistake - not by a raw error of the database or of the first query reaching it."""

import pytest

from hare import fields
from hare.ddl import UniqueConstraint
from hare.exceptions import ConfigurationError
from hare.migrations.state.state_apps import StateApps
from hare.models import Model


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
