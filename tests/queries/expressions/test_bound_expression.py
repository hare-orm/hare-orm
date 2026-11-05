from collections import Counter

import pytest

from hare.fields.data.numeric.int_field import IntField
from hare.query.expressions import Case, F, Q, Value, When
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.functions import Coalesce, Length, Max
from tests.shared_models.testmodels import JSONFields, Tournament


def test_case_binds_each_condition_and_encodes_each_literal_once(db, monkeypatch):
    condition = Q(name="a")
    then, default = Value({"then": 1}), Value({"default": 2})
    counts = Counter()
    original_condition = Q.get_result
    original_literal = Value.get_encoded_result

    def get_condition(node, context):
        if node is condition:
            counts["condition"] += 1
        return original_condition(node, context)

    def get_literal(node, context, encoder):
        if node is then or node is default:
            counts["then" if node is then else "default"] += 1
        return original_literal(node, context, encoder)

    monkeypatch.setattr(Q, "get_result", get_condition)
    monkeypatch.setattr(Value, "get_encoded_result", get_literal)
    connection = Tournament._meta.connection
    references = []
    context = ExpressionContext(
        model=Tournament,
        table=Tournament._meta.basetable,
        annotations={},
        dialect=connection.dialect,
        connection=connection,
        value_wrapper_references=references,
    )
    Case(When(condition, then=then), default=default).get_result(context)
    assert counts == {"condition": 1, "then": 1, "default": 1}
    assert len(references) == 3


def test_function_selects_the_json_encoder_before_building_its_literal(db, monkeypatch):
    literal = Value("fallback")
    calls = []
    original = Value.get_encoded_result

    def get_literal(node, context, encoder):
        if node is literal:
            calls.append(encoder)
        return original(node, context, encoder)

    monkeypatch.setattr(Value, "get_encoded_result", get_literal)
    connection = JSONFields._meta.connection
    references = []
    context = ExpressionContext(
        model=JSONFields,
        table=JSONFields._meta.basetable,
        annotations={},
        dialect=connection.dialect,
        connection=connection,
        value_wrapper_references=references,
    )
    result = Coalesce(literal, F("data")).get_result(context)
    assert len(calls) == len(references) == 1
    assert result.output_field is JSONFields._meta.fields_map["data"]


def test_nested_function_uses_the_child_result_type(db):
    connection = Tournament._meta.connection
    context = ExpressionContext(
        model=Tournament,
        table=Tournament._meta.basetable,
        annotations={},
        dialect=connection.dialect,
        connection=connection,
    )
    result = Max(Length("name")).get_result(context)
    assert isinstance(result.output_field, IntField)


@pytest.mark.parametrize("reference", [F("related_name"), Length("related_name")])
def test_annotation_reference_keeps_its_required_joins(db, reference):
    from hare.query.expressions.expression_result import ExpressionResult
    from hare.sql import Table

    connection = Tournament._meta.connection
    source = Tournament._meta.basetable
    related = Table("related_book")
    joins = [(related, source.id == related.id)]

    class RelatedValue(F):
        def get_result(self, context):
            return ExpressionResult(
                term=related.name,
                joins=joins,
                output_field=Tournament._meta.fields_map["name"],
            )

    context = ExpressionContext(
        model=Tournament,
        table=source,
        annotations={"related_name": RelatedValue("name")},
        dialect=connection.dialect,
        connection=connection,
    )
    assert reference.get_result(context).joins == joins
