"""Companion model for tests/dialects/postgresql/test_citext.py - kept in its OWN module (not
models_citext.py, which also declares a plain CitextField() model) so a test loading only this
module in isolation genuinely exercises whether the citext extension gets created from THIS
model's GeneratedField-wrapped column alone, not silently masked by another model's plain
CitextField in the same app_modules load."""

from hare import Model, fields
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.dialects.postgresql.fields.citext_field import CitextField
from hare.fields.generated_field import GeneratedField


class GeneratedCitextContact(Model):
    """A citext column built via GeneratedField(output_field=CitextField(...)) - requires the
    same `citext` extension a plain CitextField() does, which used to never get created for the
    generated-column case (GeneratedField.output_field's own requires_extension was never
    delegated)."""

    id = fields.IntField(primary_key=True)
    email = fields.CharField(max_length=100)
    email_citext = GeneratedField(expression=RawSQLTerm("email"), output_field=CitextField())

    class Meta:
        table = "generated_citext_contact"
