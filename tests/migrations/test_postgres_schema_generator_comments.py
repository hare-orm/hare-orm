"""_table_comment_generator must dedup like _column_comment_generator already
does - otherwise a table comment emitted twice (e.g.
once per schema-generation pass over the same model) produces a duplicate
``COMMENT ON TABLE`` statement."""

from hare.dialects.postgresql.schema.editor import PostgresqlSchemaEditor
from tests.utils.fake_client import FakeClient


def _make_generator() -> PostgresqlSchemaEditor:
    generator = object.__new__(PostgresqlSchemaEditor)
    generator.comments_array = []
    generator.client = FakeClient("postgresql")
    return generator


def test_table_comment_generator_dedups_identical_comments():
    generator = _make_generator()
    generator._get_table_comment_sql("widget", "a widget")
    generator._get_table_comment_sql("widget", "a widget")
    assert len(generator.comments_array) == 1


def test_table_comment_generator_keeps_distinct_comments():
    generator = _make_generator()
    generator._get_table_comment_sql("widget", "a widget")
    generator._get_table_comment_sql("gadget", "a gadget")
    assert len(generator.comments_array) == 2


def test_column_comment_generator_still_dedups():
    generator = _make_generator()
    generator._get_column_comment_sql("widget", "name", "the name")
    generator._get_column_comment_sql("widget", "name", "the name")
    assert len(generator.comments_array) == 1
