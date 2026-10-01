from hare import Model, fields
from hare.dialects.postgresql.fields.vector import VectorField
from hare.dialects.postgresql.indexes import HnswIndex, IvfflatIndex


class VectorEntry(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    embedding = VectorField(dimensions=3)

    class Meta:
        table = "vector_entry"
        # Three unnamed indexes on the one column - differing only in access method, operator
        # class and storage parameters, each gets its own generated name.
        indexes = (
            HnswIndex(fields=("embedding",), m=16, ef_construction=64, opclasses=("vector_l2_ops",)),
            HnswIndex(fields=("embedding",), m=16, ef_construction=64, opclasses=("vector_cosine_ops",)),
            IvfflatIndex(fields=("embedding",), lists=1, opclasses=("vector_l2_ops",)),
        )
