"""The rename questions of makemigrations: asked only of the one removed and one added field, model or
app's model declared alike; a yes renames or moves it, a no - or no answer - removes and adds it with a
warning; an answer given beforehand decides without asking."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from hare import fields
from hare.fields.encrypted import EncryptedTextField
from hare.fields.field import Field
from hare.migrations.autodetection.operation_generator import OperationGenerator
from hare.migrations.autodetection.rename_questions import RenameQuestions
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State
from hare.models import Model

pytestmark = pytest.mark.database_independent


class RecordedRenameQuestions(RenameQuestions):
    """Answers every question it's asked the same, recording the questions."""

    def __init__(self, answer: bool, renames: Mapping[str, str] | None = None) -> None:
        super().__init__(renames)
        self.answer = answer
        self.questions: list[str] = []

    def ask(self, question: str) -> bool:
        self.questions.append(question)
        return self.answer


def make_model(
    model_name: str, app_label: str = "shop", table: str | None = None, **model_fields: Field
) -> type[Model]:
    attributes: dict[str, Any] = {"id": fields.IntField(primary_key=True), **model_fields}
    meta_options = {"app": app_label} if table is None else {"app": app_label, "table": table}
    attributes["Meta"] = type("Meta", (), meta_options)
    return type(model_name, (Model,), attributes)


def build_state(*models: type[Model]) -> State:
    state = State()
    for model in models:
        app_label = model.Meta.app  # type: ignore[attr-defined]
        state.models[(app_label, model.__name__)] = ModelState.make_from_model(app_label, model)
    return state


def get_types(operations: list[Any]) -> list[str]:
    return sorted(type(operation).__name__ for operation in operations)


@pytest.mark.parametrize("answer", [True, False])
def test_a_removed_and_an_added_field_declared_alike_are_asked_about_once(answer: bool) -> None:
    rename_questions = RecordedRenameQuestions(answer)
    generator = OperationGenerator(
        build_state(make_model("Item", title=fields.CharField(max_length=20))),
        build_state(make_model("Item", name=fields.CharField(max_length=20))),
        rename_questions=rename_questions,
    )

    operations = generator.generate()

    assert rename_questions.questions == ["Was field 'title' of shop.Item renamed to 'name'?"]
    if answer:
        assert get_types(operations) == ["RenameField"]
        assert not generator.warnings
    else:
        assert get_types(operations) == ["AddField", "RemoveField"]
        assert any("--rename shop.Item.title=name" in warning for warning in generator.warnings)


def test_fields_declared_differently_are_not_asked_about() -> None:
    rename_questions = RecordedRenameQuestions(True)
    operations = OperationGenerator(
        build_state(make_model("Item", title=fields.CharField(max_length=20))),
        build_state(make_model("Item", name=fields.CharField(max_length=30))),
        rename_questions=rename_questions,
    ).generate()

    assert rename_questions.questions == []
    assert get_types(operations) == ["AddField", "RemoveField"]


@pytest.mark.parametrize(
    ("old_fields", "new_fields"),
    [
        pytest.param(
            {"title": fields.IntField(), "code": fields.IntField()}, {"name": fields.IntField()}, id="removed"
        ),
        pytest.param({"title": fields.IntField()}, {"name": fields.IntField(), "code": fields.IntField()}, id="added"),
    ],
)
def test_several_fields_declared_alike_on_one_side_are_not_asked_about(
    old_fields: dict[str, Field], new_fields: dict[str, Field]
) -> None:
    rename_questions = RecordedRenameQuestions(True)
    operations = OperationGenerator(
        build_state(make_model("Item", **old_fields)),
        build_state(make_model("Item", **new_fields)),
        rename_questions=rename_questions,
    ).generate()

    assert rename_questions.questions == []
    assert "RenameField" not in get_types(operations)


@pytest.mark.parametrize("answer", [True, False])
def test_a_removed_and_an_added_model_declared_alike_are_asked_about_once(answer: bool) -> None:
    rename_questions = RecordedRenameQuestions(answer)
    generator = OperationGenerator(
        build_state(make_model("Item", name=fields.CharField(max_length=20))),
        build_state(make_model("Product", name=fields.CharField(max_length=20))),
        rename_questions=rename_questions,
    )

    operations = generator.generate()

    assert rename_questions.questions == ["Was model shop.Item renamed to Product?"]
    if answer:
        assert get_types(operations) == ["RenameModel"]
    else:
        assert get_types(operations) == ["CreateModel", "DeleteModel"]
        assert any("--rename shop.<old>=Product" in warning for warning in generator.warnings)


def test_a_model_renamed_with_a_field_added_is_not_asked_about() -> None:
    rename_questions = RecordedRenameQuestions(True)
    operations = OperationGenerator(
        build_state(make_model("Item", name=fields.CharField(max_length=20))),
        build_state(make_model("Product", name=fields.CharField(max_length=20), price=fields.IntField(null=True))),
        rename_questions=rename_questions,
    ).generate()

    assert rename_questions.questions == []
    assert get_types(operations) == ["CreateModel", "DeleteModel"]


@pytest.mark.parametrize("answer", [True, False])
def test_a_model_moved_to_another_app_with_its_table_is_asked_about_once(answer: bool) -> None:
    rename_questions = RecordedRenameQuestions(answer)
    old_state = build_state(make_model("Tag", "blog", table="tag", name=fields.CharField(max_length=20)))
    new_state = build_state(make_model("Tag", "catalog", table="tag", name=fields.CharField(max_length=20)))

    operations = OperationGenerator(old_state, new_state, rename_questions=rename_questions).generate()

    assert rename_questions.questions == ["Was model blog.Tag moved to catalog.Tag, keeping its table?"]
    created, deleted = (
        next(operation for operation in operations if type(operation).__name__ == operation_type)
        for operation_type in ("CreateModel", "DeleteModel")
    )
    assert created.state_only is answer
    assert deleted.state_only is answer


def test_an_answer_given_beforehand_decides_without_asking() -> None:
    rename_questions = RecordedRenameQuestions(False, {"shop.Product.title": "name", "shop.Item": "Product"})
    operations = OperationGenerator(
        build_state(make_model("Item", title=fields.CharField(max_length=20))),
        build_state(make_model("Product", name=fields.CharField(max_length=20))),
        rename_questions=rename_questions,
    ).generate()

    assert rename_questions.questions == []
    assert get_types(operations) == ["RenameField", "RenameModel"]


def test_an_answer_given_beforehand_naming_another_field_is_a_no() -> None:
    rename_questions = RecordedRenameQuestions(True, {"shop.Item.title": "label"})
    operations = OperationGenerator(
        build_state(make_model("Item", title=fields.CharField(max_length=20))),
        build_state(make_model("Item", name=fields.CharField(max_length=20))),
        rename_questions=rename_questions,
    ).generate()

    assert rename_questions.questions == []
    assert get_types(operations) == ["AddField", "RemoveField"]


def test_no_rename_questions_rename_nothing() -> None:
    operations = OperationGenerator(
        build_state(make_model("Item", title=fields.CharField(max_length=20))),
        build_state(make_model("Item", name=fields.CharField(max_length=20))),
    ).generate()

    assert get_types(operations) == ["AddField", "RemoveField"]


@pytest.mark.parametrize("answer_given_beforehand", [True, False])
def test_a_renamed_encrypted_field_renames_its_blind_index_with_it(answer_given_beforehand: bool) -> None:
    rename_questions = RecordedRenameQuestions(True, {"shop.Item.email": "mail"} if answer_given_beforehand else None)
    operations = OperationGenerator(
        build_state(make_model("Item", email=EncryptedTextField(blind_index=True, null=True))),
        build_state(make_model("Item", mail=EncryptedTextField(blind_index=True, null=True))),
        rename_questions=rename_questions,
    ).generate()

    assert sorted((operation.old_name, operation.new_name) for operation in operations) == [
        ("email", "mail"),
        ("email_blind_index", "mail_blind_index"),
    ]
    assert get_types(operations) == ["RenameField", "RenameField"]


def test_a_model_with_an_expression_in_its_options_is_asked_about() -> None:
    """A model whose table options hold an expression of hare (a partition key) was never asked about - the
    expressions of the two states compared by identity."""
    from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
    from hare.query.functions import Trunc

    def make_partitioned_model(model_name: str) -> type[Model]:
        table_options = [ClickhouseTableOptions(partition_by=Trunc("created", "month"))]
        meta = type("Meta", (), {"app": "shop", "table_options": table_options})
        attributes = {"id": fields.IntField(primary_key=True), "created": fields.DatetimeField(), "Meta": meta}
        return type(model_name, (Model,), attributes)

    rename_questions = RecordedRenameQuestions(True)
    operations = OperationGenerator(
        build_state(make_partitioned_model("Item")),
        build_state(make_partitioned_model("Product")),
        rename_questions=rename_questions,
    ).generate()

    assert rename_questions.questions == ["Was model shop.Item renamed to Product?"]
    assert get_types(operations) == ["RenameModel"]


def test_options_ahead_of_a_field_rename_name_the_field_by_its_old_name() -> None:
    """A field renamed in a ClickHouse sorting key beside a projection reading it: the options holding the
    table while the field changed went ahead of the RenameField naming the new field - migrate refused them,
    the field not renamed yet."""
    from hare.ddl.raw_sql_term import RawSQLTerm
    from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
    from hare.dialects.clickhouse.schema_objects.clickhouse_projection import ClickhouseProjection
    from hare.migrations.operations import AlterModelOptions, RenameField

    def make_sorted_model(site_name: str) -> type[Model]:
        projection = ClickhouseProjection(
            "by_site", RawSQLTerm(f"SELECT {site_name}, count() GROUP BY {site_name}"), read_columns=(site_name,)
        )
        table_options = [ClickhouseTableOptions(order_by=("id", site_name), projections=(projection,))]
        meta = type("Meta", (), {"app": "shop", "table_options": table_options})
        attributes = {
            "id": fields.IntField(primary_key=True),
            site_name: fields.CharField(max_length=20),
            "Meta": meta,
        }
        return type("Hit", (Model,), attributes)

    operations = OperationGenerator(
        build_state(make_sorted_model("site")),
        build_state(make_sorted_model("host")),
        rename_questions=RecordedRenameQuestions(True),
    ).generate()

    rename_position = next(
        position for position, operation in enumerate(operations) if isinstance(operation, RenameField)
    )
    (options_ahead,) = [
        operation for operation in operations[:rename_position] if isinstance(operation, AlterModelOptions)
    ]
    (table_options,) = options_ahead.options["table_options"]
    assert (table_options.order_by, table_options.projections) == (("id", "site"), ())


def test_answers_given_beforehand_pair_several_models_declared_alike() -> None:
    """Two models declared alike renamed at once: the warning names the --rename that renames each, which
    renamed nothing - nothing was asked of a group, and the answers given beforehand weren't read either."""
    rename_questions = RecordedRenameQuestions(True, {"shop.Item": "Product", "shop.Part": "Piece"})
    generator = OperationGenerator(
        build_state(make_model("Item", name=fields.IntField()), make_model("Part", name=fields.IntField())),
        build_state(make_model("Piece", name=fields.IntField()), make_model("Product", name=fields.IntField())),
        rename_questions=rename_questions,
    )
    operations = generator.generate()

    assert rename_questions.questions == []
    assert sorted((operation.old_name, operation.new_name) for operation in operations) == [
        ("Item", "Product"),
        ("Part", "Piece"),
    ]
    rename_questions.raise_if_renames_unapplied()


def test_answers_given_beforehand_pair_several_fields_declared_alike() -> None:
    rename_questions = RecordedRenameQuestions(True, {"shop.Item.x": "x2", "shop.Item.y": "y2"})
    operations = OperationGenerator(
        build_state(make_model("Item", x=fields.IntField(), y=fields.IntField())),
        build_state(make_model("Item", x2=fields.IntField(), y2=fields.IntField())),
        rename_questions=rename_questions,
    ).generate()

    assert rename_questions.questions == []
    assert sorted((operation.old_name, operation.new_name) for operation in operations) == [("x", "x2"), ("y", "y2")]
