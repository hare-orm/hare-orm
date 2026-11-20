from types import SimpleNamespace

import pytest

from hare import Model, fields
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.dialects.dialect_registry import DialectRegistry
from hare.models.write.bound_rows import BoundRows
from hare.query.statements.write.bulk.create.bulk_create_query import BulkCreateQuery
from hare.query.statements.write.bulk.create.conflict_clause import ConflictClause
from tests.utils.model_bindings import ModelBindings

pytestmark = pytest.mark.database_independent


@pytest.mark.parametrize("composite", [False, True])
@pytest.mark.parametrize("named", [False, True])
@pytest.mark.parametrize("nulls_distinct", [None, True, False])
def test_null_conflict_keys_follow_the_target_constraint(composite, named, nulls_distinct):
    names = ("code", "edition") if composite else ("code",)

    class Book(Model):
        code = fields.TextField(null=True)
        edition = fields.IntField()
        title = fields.TextField()

        class Meta:
            constraints = [UniqueConstraint(fields=names, name="book_key", nulls_distinct=nulls_distinct)]

    prepared_models = ModelBindings.prepare(Book)

    dialect = DialectRegistry.get_dialect("sqlite")
    bulk_create = SimpleNamespace(
        _binding=prepared_models.get_binding(Book),
        _context=None,
        model=Book,
        dialect=dialect,
        _update_fields=["title"],
        _on_conflict=None if named else names,
        _on_conflict_constraint="book_key" if named else None,
        get_conflict_field_names=BulkCreateQuery.get_conflict_field_names,
    )
    objects = [
        prepared_models.new(Book, code=None, edition=1, title="first"),
        prepared_models.new(Book, code="B", edition=1, title="middle"),
        prepared_models.new(Book, code=None, edition=1, title="last"),
    ]
    rows = BoundRows(("code", "edition", "title"), [(obj.code, obj.edition, obj.title) for obj in objects])
    groups = ConflictClause.split_by_conflict_key(bulk_create, rows)
    assert [[obj.title for obj in objects[group]] for group in groups] == (
        [["first", "middle"], ["last"]] if nulls_distinct is False else [["first", "middle", "last"]]
    )
