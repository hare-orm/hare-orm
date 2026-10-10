"""Models for the abstract Meta.indexes/constraints tuple inheritance regression tests."""

from hare import fields
from hare.ddl.constraints import UniqueConstraint
from hare.ddl.indexes import Index
from hare.models import Model
from hare.query.expressions import F
from hare.query.functions import Lower


class TupleIndexBase(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50)

    class Meta:
        abstract = True
        indexes = (Index(Lower(F("title"))),)
        constraints = (UniqueConstraint(fields=("title",)),)


class TupleIndexPlain(TupleIndexBase):
    class Meta:
        table = "tuple_index_plain"


class TupleIndexRenamedColumn(TupleIndexBase):
    title = fields.CharField(max_length=50, source_field="title_renamed")

    class Meta:
        table = "tuple_index_renamed_column"


class TupleIndexLeftBranch(TupleIndexBase):
    class Meta:
        abstract = True


class TupleIndexRightBranch(TupleIndexBase):
    class Meta:
        abstract = True


class TupleIndexDiamond(TupleIndexLeftBranch, TupleIndexRightBranch):
    class Meta:
        table = "tuple_index_diamond"
