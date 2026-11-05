"""Column names come from initialized metadata, including relation key fields and source overrides.

The db fixture initializes relationships before their database columns are inspected.
"""

import pytest

from tests.shared_models.testmodels import Book, UUIDFkRelatedSourceModel


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
    assert model_class._meta.get_column_names([field_name]) == [column]
