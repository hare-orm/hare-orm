from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
from tests.utils.empty_table_comments import EmptyTableComments


class TestSchemaEditor(BaseSchemaEditor):
    __test__ = False

    table_comments_class = EmptyTableComments
