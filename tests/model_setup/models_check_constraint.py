"""A Meta.constraints CheckConstraint model, kept in its own module so generate_schemas()
against it (SQLite has no ALTER TABLE ADD CONSTRAINT of any type, unlike UniqueConstraint's own
CREATE UNIQUE INDEX fallback) doesn't affect any other fixture module's schema."""

from hare import fields
from hare.ddl import RawSQLTerm
from hare.ddl.constraints import CheckConstraint
from hare.models import Model


class WidgetWithCheckConstraint(Model):
    id = fields.IntField(primary_key=True)
    age = fields.IntField()

    class Meta:
        constraints = [CheckConstraint(check=RawSQLTerm("age >= 0"), name="age_non_negative")]
