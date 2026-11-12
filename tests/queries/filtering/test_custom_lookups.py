import operator
from types import SimpleNamespace

import pytest
import pytest_asyncio

from hare.contrib.test import requires_features
from hare.fields import CharField, IntField
from hare.query.enums import Lookup
from hare.query.expressions import Q
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.filters import FieldLookup
from hare.query.filters.lookups.field_lookups import FieldLookups
from hare.query.filters.lookups.lookups import Lookups
from hare.query.filters.resolution.annotation_filters import AnnotationFilters
from hare.query.filters.resolution.relation_filters import RelationFilters
from hare.query.filters.resolution.to_many_filters import ToManyFilters
from hare.query.lookup_info.lookup_info_builder import LookupInfoBuilder
from hare.sql import Table
from hare.sql.terms import Term
from tests.shared_models.testmodels import CharFields, Event, Tournament


def _reversed_lookup(field: CharField | None) -> FieldLookup:
    def _operator(term: Term, value: str) -> Term:
        return term == value[::-1]

    return FieldLookup(_operator)


CharField.register_lookup("reversed", _reversed_lookup)


@pytest_asyncio.fixture
async def char_fields_data(db):
    await CharFields.objects.create(char="moo")
    await CharFields.objects.create(char="oom")


@requires_features(identifier_quote_char='"')
@pytest.mark.asyncio
async def test_register_lookup_generates_sql(db, char_fields_data):
    sql = CharFields.objects.filter(char__reversed="oom").sql()
    assert '"char"' in sql


@pytest.mark.asyncio
async def test_register_lookup_filters_correctly(db, char_fields_data):
    assert set(await CharFields.objects.filter(char__reversed="oom").values_list("char", flat=True)) == {"moo"}


@pytest.mark.asyncio
async def test_register_lookup_does_not_shadow_builtin_suffixes(db, char_fields_data):
    assert set(await CharFields.objects.filter(char__not="moo").values_list("char", flat=True)) == {"oom"}


@pytest.mark.parametrize("model,relation_name", [(Event, "participants"), (Tournament, "events")])
def test_relation_lookup_uses_the_encoder_builder(db, model, relation_name):
    relation = model._meta.fields_map[relation_name]
    connection = model.get_connection()
    context = ExpressionContext(model, model._meta.basetable, {}, dialect=connection.dialect, connection=connection)
    built = []

    def encode(value, instance, field, dialect):
        assert instance is model and field is relation and dialect is connection.dialect
        return value + 10

    def build(field, dialect):
        built.append((field, dialect))
        return encode

    def unused_encoder(*args):
        raise AssertionError("The builder's encoder takes precedence")

    lookup = FieldLookup(operator.eq, unused_encoder, value_encoder_builder=build)
    info = SimpleNamespace(relations=(relation,), field_lookup=lookup)
    criterion, _, selected_encoder, _, encoded = RelationFilters.get_relation_lookup_criterion(
        context, info, 1, context.table, None
    )
    assert built == [(relation, connection.dialect)]
    assert selected_encoder is encode
    assert encoded == criterion.right.value == 11


def test_through_table_records_the_encoder_used_to_build_its_filter(db, monkeypatch):
    relation = Event._meta.fields_map["participants"]
    connection = Event.get_connection()
    context = ExpressionContext(Event, Event._meta.basetable, {}, dialect=connection.dialect, connection=connection)
    built = []

    def encode(value, instance, field, dialect):
        return value + 10

    def build(field, dialect):
        built.append((field, dialect))
        return encode

    lookups = FieldLookups.get(relation)
    LookupInfoBuilder.forget_descriptions()
    try:
        with monkeypatch.context() as patch:
            patch.setitem(lookups, Lookup.EXACT, lookups[Lookup.EXACT].with_changes(value_encoder_builder=build))
            modifier, reference = ToManyFilters.get_through_table_kwarg(
                Q(participants=1),
                context,
                "participants",
                1,
                relation,
                reads_plain_value=True,
                converts_lists=False,
            )
            assert built == [(relation, connection.dialect)]
            assert reference is not None
            assert reference.reference.value_encoder is encode
            assert modifier.where_criterion.right.value == 11
    finally:
        LookupInfoBuilder.forget_descriptions()


def test_annotation_list_records_its_selected_encoder_without_rebuilding(db):
    connection = Event.get_connection()
    context = ExpressionContext(Event, Event._meta.basetable, {}, dialect=connection.dialect, connection=connection)
    built = []
    field = IntField()

    def encode(values, instance, field, dialect):
        return [value + 10 for value in values]

    def build(field, dialect):
        built.append((field, dialect))
        return encode

    lookup = FieldLookup(Lookups.is_in, value_encoder_builder=build)
    raw = [1, 2]
    convert, encoder_field, selected_encoder = AnnotationFilters.get_parameter_converter(context, lookup, field, raw)
    assert convert is not None
    encoded = convert(raw)
    table = Table("book")
    criterion = lookup.operator(table.id, encoded)
    reference = AnnotationFilters.get_value_reference(
        context, criterion, lookup.operator, raw, encoded, selected_encoder, encoder_field, convert
    )
    assert reference is not None
    assert reference.value_encoder is encode
    assert built == [(field, connection.dialect)]
