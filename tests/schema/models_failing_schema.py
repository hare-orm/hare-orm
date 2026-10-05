"""A model whose DDL the database rejects - schema generation fails after the database exists."""

from hare import fields
from hare.ddl import RawSQLTerm
from hare.ddl.constraints import CheckConstraint
from hare.models import Model


class RejectedByDatabase(Model):
    id = fields.IntField(primary_key=True)

    class Meta:
        table = "rejected_by_database"
        constraints = (CheckConstraint(check=RawSQLTerm("no_such_column > 0"), name="rejected_by_database_check"),)
