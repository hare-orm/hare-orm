from types import SimpleNamespace

import pytest

from hare import Model, fields
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.dialects.dialect_registry import DialectRegistry
from hare.query.statements.write.bulk.create.bulk_create_query import BulkCreateQuery
from hare.query.statements.write.bulk.create.conflict_clause import ConflictClause

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

    dialect = DialectRegistry.get_dialect("sqlite")
    bulk_create = SimpleNamespace(
        model=Book,
        dialect=dialect,
        _update_fields=["title"],
        _on_conflict=None if named else names,
        _on_conflict_constraint="book_key" if named else None,
        get_conflict_field_names=BulkCreateQuery.get_conflict_field_names,
    )
    objects = [
        Book(code=None, edition=1, title="first"),
        Book(code="B", edition=1, title="middle"),
        Book(code=None, edition=1, title="last"),
    ]
    groups = ConflictClause.split_by_conflict_key(bulk_create, objects)
    assert [[obj.title for obj in group] for group in groups] == (
        [["first", "middle"], ["last"]] if nulls_distinct is False else [["first", "middle", "last"]]
    )
