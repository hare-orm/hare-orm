"""Repro/regression tests for hare/dialects/base/schema/constraints.py's
_get_fields_to_columns() finding.

Needs the `db` fixture (not just importing the model class) - apps.py's init_foreign_key_or_one_to_one_field only
runs once the app has actually finished initializing, and it rewrites an FK/O2O field's OWN
.source_field to the key-column's field name ("<field>_id" by default) regardless of any custom
source_field the field was declared with; the real custom override moves to THAT key field's own
.source_field instead. Migrations/schema-editor code always runs against a fully-initialized app
in practice, so that's the state that actually matters here.
"""

import pytest

from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
from tests.shared_models.testmodels import Book, UUIDFkRelatedSourceModel
from tests.utils.fake_client import FakeClient


def make_editor() -> BaseSchemaEditor:
    return BaseSchemaEditor(FakeClient("sql"))


@pytest.mark.parametrize(
    ("model_class", "field_name", "column"),
    [
        # `field_object.source_field or field_name` returned the bare FK field name ("author") instead
        # of its real DB column ("author_id"), producing a constraint on a column that doesn't exist.
        pytest.param(Book, "author", "author_id", id="fk_uses_id_suffix_convention"),
        # An explicit source_field override must still win over the "<field>_id" convention.
        pytest.param(UUIDFkRelatedSourceModel, "model", "d", id="fk_respects_custom_source_field"),
        pytest.param(Book, "name", "name", id="regular_field_unaffected"),
    ],
)
@pytest.mark.asyncio
async def test_resolve_fields_to_columns(db, model_class, field_name, column):
    editor = make_editor()
    assert editor.constraint_names.get_fields_to_columns(model_class, [field_name]) == [column]
