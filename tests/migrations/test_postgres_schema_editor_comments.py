"""_get_table_comment_sql must dedup like _get_column_comment_sql already does - the same
asymmetry BasePostgresSchemaGenerator._table_comment_generator had before it was fixed (see
test_postgres_schema_generator_comments.py) existed independently on the migrations/schema
editor side too."""

from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor
from tests.utils.fake_client import FakeClient


def _make_editor() -> PostgresqlSchemaEditor:
    editor = object.__new__(PostgresqlSchemaEditor)
    editor.comments_array = []
    editor.client = FakeClient("postgresql")
    editor.table_comments = editor.table_comments_class(editor)
    return editor


def test_table_comment_sql_dedups_identical_comments():
    editor = _make_editor()
    editor.table_comments.get_table_comment_sql("widget", "a widget")
    editor.table_comments.get_table_comment_sql("widget", "a widget")
    assert len(editor.comments_array) == 1


def test_table_comment_sql_keeps_distinct_comments():
    editor = _make_editor()
    editor.table_comments.get_table_comment_sql("widget", "a widget")
    editor.table_comments.get_table_comment_sql("gadget", "a gadget")
    assert len(editor.comments_array) == 2


def test_column_comment_sql_still_dedups():
    editor = _make_editor()
    editor.table_comments.get_column_comment_sql("widget", "name", "the name")
    editor.table_comments.get_column_comment_sql("widget", "name", "the name")
    assert len(editor.comments_array) == 1
