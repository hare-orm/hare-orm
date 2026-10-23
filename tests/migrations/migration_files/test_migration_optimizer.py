"""MigrationOptimizer folds the operations of squashed migrations - and never moves one across
another touching a common model."""

import pytest

from hare import fields
from hare.ddl.indexes.index import Index
from hare.ddl.raw_sql_term import RawSQLTerm
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

pytestmark = pytest.mark.database_independent


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


def test_added_fields_with_their_own_index_fold_into_the_created_model():
    result = optimize(
        create_book(),
        AddField("Book", "pages", fields.IntField(null=True, db_index=True)),
        AddField("Book", "sequel", fields.ForeignKeyField("app.Book", related_name="prequels", null=True)),
        AddField("Book", "price", fields.IntField(null=True, db_index=True)),
    )
    # A field's own index is its flag - the created model has it with the field.
    assert describe(result) == ["CreateModel"]
    assert [name for name, _field in result[0].fields] == ["id", "title", "pages", "sequel", "price"]
    assert [field.index for _name, field in result[0].fields[2:]] == [True, True, True]


def test_an_added_field_altered_to_have_its_own_index_folds_into_one_add_field():
    result = optimize(
        AddField("Book", "pages", fields.IntField(null=True)),
        AlterField("Book", "pages", fields.IntField(null=True, db_index=True)),
    )
    assert describe(result) == ["AddField"]
    assert result[0].field.index


def test_a_created_model_takes_the_table_and_its_own_relations_of_a_rename():
    created = CreateModel(
        "Book",
        [
            ("id", fields.IntField(primary_key=True)),
            ("sequel", fields.ForeignKeyField("app.Book", related_name="prequels", null=True)),
            ("author", fields.ForeignKeyField("app.Author", related_name="books", null=True)),
        ],
        options={"table": "book", "table_is_explicit": False},
    )
    result = optimize(created, RenameModel("Book", "Volume"))
    assert describe(result) == ["CreateModel"]
    assert result[0].options == {"table": "volume", "table_is_explicit": False}
    assert [field.model_name for _name, field in result[0].fields[1:]] == ["app.Volume", "app.Author"]
    # The operation squashed isn't changed in place.
    assert created.fields[1][1].model_name == "app.Book"
    named = CreateModel(
        "Book", [("id", fields.IntField(primary_key=True))], options={"table": "tomes", "table_is_explicit": True}
    )
    assert optimize(named, RenameModel("Book", "Volume"))[0].options == {"table": "tomes", "table_is_explicit": True}


def test_an_added_one_to_one_relation_to_several_fields_folds_in_unique_with_it():
    pair = CreateModel(
        "Pair",
        [("a", fields.IntField()), ("b", fields.IntField())],
        options={"primary_key_attribute": ("a", "b")},
    )
    twin = AddField(
        "Pair", "twin", fields.OneToOneField("app.Pair", related_name="twin_of", null=True, to_field=("a", "b"))
    )
    # The relation's uniqueness is its own - made with it, never an operation of its own.
    result = optimize(pair, twin)
    assert describe(result) == ["CreateModel"]
    assert "constraints" not in result[0].options
    assert describe(optimize(pair, twin, RunSQL("UPDATE pair SET twin_a = a"))) == ["CreateModel", "RunSQL"]


def test_a_created_model_takes_a_field_rename_where_its_other_fields_read_the_field():
    created = CreateModel(
        "Book",
        [
            ("id", fields.IntField(primary_key=True)),
            ("code", fields.IntField(unique=True)),
            ("sequel", fields.ForeignKeyField("app.Book", related_name="prequels", null=True, to_field="code")),
            ("author", fields.ForeignKeyField("app.Author", related_name="books", null=True, to_field="code")),
        ],
    )
    result = optimize(created, RenameField("Book", "code", "number"))
    assert describe(result) == ["CreateModel"]
    renamed_fields = dict(result[0].fields)
    assert list(renamed_fields) == ["id", "number", "sequel", "author"]
    assert renamed_fields["sequel"].to_field == "number"
    # Another model's field of the same name isn't the renamed one.
    assert renamed_fields["author"].to_field == "code"
    assert dict(created.fields)["sequel"].to_field == "code"


@pytest.mark.parametrize("operation", [RenameField("Book", "code", "number"), RemoveField("Book", "code")])
def test_a_created_model_declaring_raw_sql_keeps_a_field_rename_or_removal_apart(operation):
    """Its raw SQL may name the column - the creation folded with the operation would make the table
    without it, while the database renames the column under the raw SQL."""
    created = CreateModel(
        "Book",
        [
            ("id", fields.IntField(primary_key=True)),
            ("code", fields.IntField()),
            ("double", fields.GeneratedField(expression=RawSQLTerm("code * 2"), output_field=fields.IntField())),
        ],
    )
    assert describe(optimize(created, operation)) == ["CreateModel", type(operation).__name__]


def test_a_created_models_field_altered_to_have_no_index_keeps_the_declared_indexes():
    created = CreateModel(
        "Book",
        [("id", fields.IntField(primary_key=True)), ("pages", fields.IntField(null=True, db_index=True))],
        options={"indexes": [Index(fields=("id", "pages"))]},
    )
    result = optimize(created, AlterField("Book", "pages", fields.IntField(null=True)))
    assert describe(result) == ["CreateModel"]
    assert not dict(result[0].fields)["pages"].index
    assert result[0].options == {"indexes": [Index(fields=("id", "pages"))]}
