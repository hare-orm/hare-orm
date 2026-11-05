import pytest

from hare import Model, fields
from hare.ddl.constraints import CheckConstraint, UniqueConstraint
from hare.ddl.indexes import Index, PartialIndex
from hare.declarations.declaration_values import DeclarationValues
from hare.migrations import State
from hare.migrations.autodetection.operation_generator import OperationGenerator
from hare.migrations.autodetection.rename_questions import RenameQuestions
from hare.migrations.operations import AddField, RemoveField, RenameField
from hare.migrations.state.model_state import ModelState
from hare.models.enums import ModelOption
from hare.query.expressions import F, Q

pytestmark = pytest.mark.database_independent


@pytest.fixture
def generated_state():
    state = State()
    state.models[("models", "Book")] = ModelState(
        name="Book",
        app="models",
        table="book",
        abstract=False,
        description="",
        options={},
        bases=(Model,),
        pk_field_name="id",
        fields={
            "id": fields.IntField(primary_key=True),
            "price": fields.IntField(),
            "total": fields.GeneratedField(F("price") + 1, output_field=fields.IntField()),
        },
    )
    return state


@pytest.mark.parametrize(
    "declaration",
    [
        Index(fields=("price", "total")),
        Index(F("total") + 1),
        Index(fields=("price",), include=("total",)),
        PartialIndex(fields=("price",), condition=Q(total__gt=0)),
        CheckConstraint(name="positive_total", check=Q(total__gte=0)),
        UniqueConstraint(fields=("price", "total")),
    ],
)
@pytest.mark.parametrize("change", ["expression", "column", "rename"])
def test_replaced_generated_column_restores_all_structured_dependencies(generated_state, declaration, change):
    before = generated_state.models[("models", "Book")]
    option = ModelOption.INDEXES if isinstance(declaration, Index) else ModelOption.CONSTRAINTS
    before.options[option] = (declaration,)
    target = generated_state.clone()
    after = target.models[("models", "Book")]
    name = "total"
    source_field = "computed_total" if change == "column" else None
    if change == "rename":
        RenameField("Book", "total", "amount").rename_in_model_state(after)
        name, source_field = "amount", "total"
    after.fields[name] = fields.GeneratedField(
        F("price") + 2, output_field=fields.IntField(), source_field=source_field
    )
    operations = OperationGenerator(generated_state, target).generate()
    removal = next(index for index, operation in enumerate(operations) if isinstance(operation, RemoveField))
    addition = next(index for index, operation in enumerate(operations) if isinstance(operation, AddField))
    assert removal < addition
    state = generated_state.clone()
    for operation in operations:
        operation.state_forward("models", state)
    final = state.models[("models", "Book")]
    assert set(final.fields) == set(after.fields)
    assert DeclarationValues.are_equal(final.options[option], after.options[option])
    assert before.fields["total"].source_field is None
    assert before.options[option] == (declaration,)


@pytest.mark.parametrize(
    "declaration",
    [Index(fields=("price", "total")), Index(F("price") + 1), UniqueConstraint(fields=("price", "total"))],
)
def test_unnamed_object_is_removed_using_names_before_field_rename(generated_state, declaration):
    before = generated_state.models[("models", "Book")]
    option = ModelOption.INDEXES if isinstance(declaration, Index) else ModelOption.CONSTRAINTS
    before.options[option] = (declaration,)
    target = generated_state.clone()
    after = target.models[("models", "Book")]
    RenameField("Book", "price", "cost").rename_in_model_state(after)
    after.options.pop(option)
    operations = OperationGenerator(
        generated_state, target, rename_questions=RenameQuestions({"models.Book.price": "cost"})
    ).generate()
    state = generated_state.clone()
    for operation in operations:
        operation.state_forward("models", state)
    final = state.models[("models", "Book")]
    assert "cost" in final.fields and "price" not in final.fields
    assert not final.options.get(option)
