"""PostgreSQL sets comments by statements of their own: the editor returns them with the statement they
belong to - one per table and commented column, the same on every call, nothing kept between calls."""

import pytest

from hare import fields
from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor
from hare.models import Model
from tests.utils.fake_client import FakeClient

pytestmark = pytest.mark.database_independent


class CommentedWidget(Model):
    """A widget."""

    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=20, description="the name")
    size = fields.IntField()

    class Meta:
        table = "commented_widget"
        app = "models"
        table_description = "a widget"


def get_comment_statements(sql: str) -> list[str]:
    return [line for line in sql.splitlines() if line.startswith("COMMENT ON")]


def test_create_table_returns_one_comment_statement_per_table_and_commented_column():
    editor = PostgresqlSchemaEditor(FakeClient("postgresql", inline_comment=False))

    first_sql = editor.table_creation.get_model_sql_data(CommentedWidget).table_sql
    second_sql = editor.table_creation.get_model_sql_data(CommentedWidget).table_sql

    assert get_comment_statements(first_sql) == [
        "COMMENT ON TABLE \"commented_widget\" IS 'a widget';",
        'COMMENT ON COLUMN "commented_widget"."name" IS \'the name\';',
    ]
    assert second_sql == first_sql


@pytest.mark.asyncio
async def test_an_added_column_gets_its_comment_statement():
    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)

    await editor.add_field(CommentedWidget, CommentedWidget, "name")

    assert get_comment_statements("\n".join(client.executed)) == [
        'COMMENT ON COLUMN "commented_widget"."name" IS \'the name\';'
    ]
