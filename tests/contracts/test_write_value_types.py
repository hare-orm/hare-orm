from typing import get_args

import pytest

from hare import Hare, Model, fields
from hare.exceptions import QueryError
from hare.fields.encrypted import EncryptedTextField
from hare.models.write.rules.enums import WriteType
from hare.models.write.rules.row_write_rules import RowWriteRules
from hare.query.expressions import F
from hare.stubs.model_stub import ModelStub
from hare.stubs.stub_imports import StubImports
from hare.stubs.value_type_text import ValueTypeText
from hare.typing_info.filter_value_types import FilterValueTypes
from hare.typing_info.write_value_types import WriteValueTypes


class TypeAuthor(Model):
    id = fields.UUIDField(primary_key=True)


class TypeBook(Model):
    id = fields.IntField(primary_key=True)
    author = fields.ForeignKeyField("types.TypeAuthor", related_name="books", null=True)
    amount = fields.DecimalField(max_digits=10, decimal_places=2)
    published = fields.DateField()
    version = fields.IntField(default=0)
    deleted = fields.DatetimeField(null=True)
    computed = fields.GeneratedField(F("version") + 1, output_field=fields.IntField())
    secret = EncryptedTextField(blind_index=True, null=True)

    class Meta:
        optimistic_lock_field = "version"
        soft_delete_field = "deleted"


class TypeEdition(Model):
    code = fields.UUIDField()
    revision = fields.IntField()
    pk = fields.CompositePrimaryKey("code", "revision")


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_write_contracts_share_input_conversions_and_keep_read_types_separate():
    context = await Hare.init(
        {
            "connections": {"default": "sqlite+aiosqlite://:memory:"},
            "apps": {"types": {"models": [__name__], "default_connection": "default"}},
        },
        connect=False,
    )
    try:
        author = context.apps.get_model("types.TypeAuthor")
        book = context.apps.get_model("types.TypeBook")
        edition = context.apps.get_model("types.TypeEdition")
        writes = WriteValueTypes.get_all(book)
        assert {"id", "pk", "author", "author_id", "amount", "published"} <= writes.keys()
        assert writes["author"].models == (author,) and writes["author"].nullable
        assert WriteValueTypes.get(author, "books") is None
        assert WriteValueTypes.get(book, "missing") is None
        assert str in WriteValueTypes.get(author, "pk").accepted_classes
        assert str in writes["amount"].accepted_classes
        assert int in writes["amount"].accepted_classes
        assert WriteValueTypes.get(book, "amount", accepts_expressions=True).accepts_expressions
        lookup = book._meta.get_lookup_info("published")
        assert str in FilterValueTypes.get_filter_value_type(lookup).accepted_classes
        assert not FilterValueTypes.get_selected_value_type(lookup).accepted_classes
        key = WriteValueTypes.get(edition, "pk").classes[0]
        assert str in get_args(key[0]) and key[1] is int
        text = ValueTypeText(StubImports(__name__))
        assert "str" in text.get_text(writes["amount"])
        assert "str" in text.get_text(WriteValueTypes.get(edition, "pk"))
        expected_updates = {"author", "author_id", "amount", "published", "secret"}
        updates = WriteValueTypes.get_all(book, accepts_expressions=True, write_type=WriteType.UPDATE)
        assert updates.keys() == expected_updates
        assert all(description.accepts_expressions for description in updates.values())
        stub = ModelStub(book, book.get_connection().dialect, StubImports(__name__), relation_depth=1)
        assert stub.get_update_types() == updates
        assert {"id", "pk", "version", "deleted", "computed", "secret_blind_index"} <= writes.keys()
        for name in WriteValueTypes.get_names(book):
            if name in expected_updates:
                RowWriteRules.get(book, WriteType.UPDATE, [name], ".update()", finds_rows_by_key=False)
            else:
                with pytest.raises(QueryError):
                    RowWriteRules.get(book, WriteType.UPDATE, [name], ".update()", finds_rows_by_key=False)
        assert WriteValueTypes.get_all(edition, write_type=WriteType.UPDATE).keys() == {"code", "revision"}
        for name in ("code", "revision"):
            RowWriteRules.get(edition, WriteType.UPDATE, [name], ".update()", finds_rows_by_key=False)
    finally:
        await Hare.close_connections()
        context.__exit__(None, None, None)
