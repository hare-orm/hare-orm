"""MigrationOptimizer folds the operations of squashed migrations - and never moves one across
another touching a common model."""

from hare import fields
from hare.ddl.indexes.index import Index
from hare.migrations.making.migration_optimizer import MigrationOptimizer
from hare.migrations.operations import (
    AddField,
    AddIndex,
    AlterField,
    AlterModelOptions,
    AlterModelTable,
    CreateModel,
    DeleteModel,
    RemoveField,
    RenameField,
    RenameModel,
    RunPython,
    RunSQL,
)


def optimize(*operations):
    return MigrationOptimizer("app").optimize(list(operations))


def create_book(**extra_options):
    return CreateModel(
        "Book",
        [("id", fields.IntField(primary_key=True)), ("title", fields.CharField(max_length=50))],
        options=extra_options or None,
    )


def describe(operations):
    return [type(operation).__name__ for operation in operations]


def test_a_created_model_takes_in_later_field_changes():
    result = optimize(
        create_book(),
        AddField("Book", "pages", fields.IntField(default=0)),
        AlterField("Book", "pages", fields.BigIntField(default=1)),
        RenameField("Book", "pages", "page_count"),
        AddField("Book", "draft", fields.BooleanField(default=False)),
        RemoveField("Book", "draft"),
    )
    assert describe(result) == ["CreateModel"]
    assert [name for name, _field in result[0].fields] == ["id", "title", "page_count"]
    assert isinstance(result[0].fields[2][1], fields.BigIntField)


def test_a_created_and_deleted_model_cancels_out():
    assert optimize(create_book(), AddField("Book", "pages", fields.IntField(default=0)), DeleteModel("Book")) == []


def test_a_created_model_takes_its_rename_options_and_table():
    result = optimize(create_book(), RenameModel("Book", "Volume"), AlterModelOptions("Volume", {"ordering": ["id"]}))
    assert describe(result) == ["CreateModel"]
    assert result[0].name == "Volume"
    assert result[0].options == {"ordering": ["id"]}
    result = optimize(create_book(), AlterModelTable("Book", "books"))
    assert result[0].options == {"table": "books"}


def test_field_operations_fold_into_each_other():
    assert (
        describe(optimize(AddField("Book", "pages", fields.IntField(default=0)), RemoveField("Book", "pages"))) == []
    )
    result = optimize(
        AddField("Book", "pages", fields.IntField(default=0)), AlterField("Book", "pages", fields.BigIntField())
    )
    assert describe(result) == ["AddField"]
    assert isinstance(result[0].field, fields.BigIntField)
    result = optimize(
        AlterField("Book", "pages", fields.IntField()), AlterField("Book", "pages", fields.BigIntField())
    )
    assert describe(result) == ["AlterField"]
    assert describe(optimize(AlterField("Book", "pages", fields.IntField()), RemoveField("Book", "pages"))) == [
        "RemoveField"
    ]
    result = optimize(RenameField("Book", "a", "b"), RenameField("Book", "b", "c"))
    assert [(operation.old_name, operation.new_name) for operation in result] == [("a", "c")]
    assert optimize(RenameField("Book", "a", "b"), RenameField("Book", "b", "a")) == []
    result = optimize(RenameModel("Book", "Volume"), RenameModel("Volume", "Tome"))
    assert [(operation.old_name, operation.new_name) for operation in result] == [("Book", "Tome")]


def test_operations_of_other_models_dont_stop_a_fold():
    result = optimize(
        create_book(),
        CreateModel("Author", [("id", fields.IntField(primary_key=True))]),
        AddField("Book", "pages", fields.IntField(default=0)),
    )
    # The fold takes the later operation's place - the operation in between moves across it.
    assert [operation.name for operation in result] == ["Author", "Book"]
    assert [name for name, _field in result[1].fields] == ["id", "title", "pages"]


def test_nothing_moves_across_an_operation_touching_the_same_model():
    author = CreateModel("Author", [("id", fields.IntField(primary_key=True))])
    book = CreateModel(
        "Book",
        [
            ("id", fields.IntField(primary_key=True)),
            ("author", fields.ForeignKeyField("app.Author", related_name="books")),
        ],
    )
    author_name = AddField("Author", "name", fields.CharField(max_length=50, default=""))
    # Book relates to Author, so Author's field can't fold into Author's creation across it.
    assert describe(optimize(author, book, author_name)) == ["CreateModel", "CreateModel", "AddField"]
    # Once the relation is gone, Author's creation and deletion cancel out across Book.
    result = optimize(author, book, RemoveField("Book", "author"), DeleteModel("Author"))
    assert [operation.name for operation in result] == ["Book"]
    assert [name for name, _field in result[0].fields] == ["id"]


def test_a_data_migration_blocks_every_fold_across_it():
    for data_operation in (RunPython(RunPython.noop), RunSQL("UPDATE book SET pages = 1")):
        result = optimize(create_book(), data_operation, AddField("Book", "pages", fields.IntField(default=0)))
        assert describe(result) == ["CreateModel", type(data_operation).__name__, "AddField"]


def test_a_field_named_in_the_options_stays_out_of_the_created_model():
    created = create_book(indexes=[Index(fields=("title",))])
    assert describe(optimize(created, RemoveField("Book", "title"))) == ["CreateModel", "RemoveField"]
    assert describe(optimize(created, RenameField("Book", "title", "name"))) == ["CreateModel", "RenameField"]


def test_a_trigger_reaches_every_model():
    index = AddIndex("Book", Index(fields=("title",), name="book_title_idx"))
    assert index.get_referenced_model_labels("app") == frozenset({"app.book"})
    assert describe(optimize(create_book(), index, AddField("Book", "pages", fields.IntField(default=0)))) == [
        "CreateModel",
        "AddIndex",
        "AddField",
    ]
