"""Expression declarations distinguish references, literal values and query scopes."""

import pytest

from hare import fields
from hare.ddl.conditions.constraint_condition import ConstraintCondition
from hare.ddl.expressions.expression_field_renaming import ExpressionFieldRenaming
from hare.exceptions import ConfigurationError, QueryError
from hare.migrations.state.state_apps import StateApps
from hare.models import Model
from hare.query.expressions import Case, Exists, F, Function, OuterReference, Q, Value, When, Window
from hare.query.expressions.structure.enums import ArgumentRole
from hare.query.expressions.structure.expression_structure import ExpressionStructure
from hare.query.expressions.structure.expression_traversal import ExpressionTraversal
from hare.query.functions import Cast, Collate, Greatest, JSONArray, JSONObject, Lower, Replace, Sum
from hare.query.functions.window import Lag
from hare.search import SearchHeadline, SearchRank, SearchVector

pytestmark = pytest.mark.database_independent


@pytest.mark.parametrize(
    ("expression", "expected_args", "expected_kwargs"),
    [
        (F("title"), ["heading"], {}),
        (Replace("title", "title", "title"), ["heading", "title", "title"], {}),
        (Cast(expression="title", output_field=fields.TextField()), [], None),
        (Collate(expression="title", collation="title"), [], {"expression": "heading", "collation": "title"}),
        (JSONObject(title="title", literal=Value("title")), [], None),
        (JSONArray("title", Value("title")), None, {}),
        (Greatest("title", Value("title")), None, {}),
        (SearchVector("title", config="title", weight="A"), ["heading"], {"config": "title", "weight": "A"}),
        (SearchHeadline("title", "title", config="title"), ["heading", "title"], {"config": "title"}),
        (SearchRank("title", "title", weights={"title": 1.0}), ["heading", "title"], {"weights": {"heading": 1.0}}),
    ],
)
def test_constructor_positions_distinguish_references_from_identical_literals(
    expression, expected_args, expected_kwargs
):
    renamed = ExpressionFieldRenaming.get_renamed(expression, "title", "heading")
    _, args, kwargs = renamed.deconstruct()
    if expected_args is not None:
        assert args == expected_args
    if expected_kwargs is not None:
        assert kwargs == expected_kwargs
    if isinstance(expression, Cast):
        assert renamed.field.name == "heading"
        assert kwargs["output_field"] is expression.cast_output_field
    if isinstance(expression, JSONObject) and not isinstance(expression, JSONArray):
        assert kwargs["title"] == "heading"
        assert kwargs["literal"].value == "title"
    if isinstance(expression, (JSONArray, Greatest)):
        assert args[0] == "heading"
        assert args[1].value == "title"
    assert ExpressionFieldRenaming.get_renamed(expression, "absent", "heading") is expression


def test_nested_conditions_and_window_rebuild_derived_state():
    branch = When(Q(title__exact=F("title")), then=F("title"), negate=True)
    expression = Window(
        Lag(Case(branch, default="title"), default="title"),
        partition_by=["title"],
        order_by=[F("title").desc(nulls_first=True)],
    )
    renamed = ExpressionFieldRenaming.get_renamed(expression, "title", "heading")
    assert renamed.partition_by == ("heading",)
    assert renamed.order_by[0].target == "heading"
    assert renamed.order_by[0].order == expression.order_by[0].order
    assert renamed.expression.default == "title"
    result_case = renamed.expression.field
    assert result_case.default == "title"
    result_branch = result_case.deconstruct()[1][0]
    assert result_branch.then.name == "heading"
    assert result_branch.args[0].filters["heading__exact"].name == "heading"
    assert result_branch.conditions[0]._is_negated
    assert expression.partition_by == ("title",)


def test_custom_expression_declares_keyword_roles_independently_of_argument_order():
    class Suffix(Function):
        structure = ExpressionStructure(keywords=(("source", ArgumentRole.FIELD),))

        def __init__(self, *, suffix, source):
            super().__init__(source, suffix)

    expression = Suffix(suffix="title", source="title")
    renamed = ExpressionFieldRenaming.get_renamed(expression, "title", "heading")
    assert renamed.field == "heading"
    assert renamed.default_values == ("title",)
    assert renamed.deconstruct()[2] == {"suffix": "title", "source": "heading"}


@pytest.mark.parametrize("literal", [{"title": F("title")}, Value({"title": F("title")})])
def test_outer_reference_and_value_are_not_local_columns(literal):
    expression = Q(title=OuterReference("title"), data=literal, title__in=["title"], other=F("title"))
    renamed = ExpressionFieldRenaming.get_renamed(expression, "title", "heading")
    assert renamed.filters["heading"].field == "title"
    assert renamed.filters["data"] is expression.filters["data"]
    assert renamed.filters["heading__in"] is expression.filters["title__in"]
    assert renamed.filters["other"].name == "heading"
    assert ExpressionFieldRenaming.get_all_read_field_names(expression) == {"title", "data", "other"}


def test_shared_deep_children_are_not_cycles():
    reference = F("title")
    expression = [reference, reference]
    for _ in range(2000):
        expression = [expression]
    renamed = ExpressionFieldRenaming.get_renamed(expression, "title", "heading")
    for _ in range(2000):
        renamed = renamed[0]
    assert [item.name for item in renamed] == ["heading", "heading"]
    assert reference.name == "title"


def test_cyclic_declaration_is_rejected_without_recursion_error():
    expression = []
    expression.append(expression)
    with pytest.raises(QueryError, match="cycle"):
        ExpressionTraversal.get_field_references(expression)


def test_constraint_dependencies_do_not_compile_expressions(monkeypatch):
    class Author(Model):
        id = fields.IntField(primary_key=True)
        name = fields.TextField()

    class Book(Model):
        id = fields.IntField(primary_key=True)
        title = fields.TextField()
        author = fields.ForeignKeyField("models.Author", null=True)

    apps = StateApps()
    for model in (Author, Book):
        apps.register_model("models", model)
    apps.init_relations()

    def unexpected_render(*args, **kwargs):
        raise AssertionError("Dependency validation must not render SQL")

    monkeypatch.setattr(Q, "get_result", unexpected_render)
    for condition in (Q(author=1), Q(author__in=[1, 2]), Q(author__isnull=True), Q(title=Lower("title"))):
        ConstraintCondition.raise_if_reads_other_columns(condition, Book)
    for condition in (Q(author__name="title"), Q(title=Sum("id")), Q(Exists(Author.objects.all()))):
        with pytest.raises(ConfigurationError, match="reads only"):
            ConstraintCondition.raise_if_reads_other_columns(condition, Book)
