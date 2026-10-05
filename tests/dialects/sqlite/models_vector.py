from hare import Model, fields
from hare.vectors import VectorField


class SqliteVectorEntry(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    embedding = VectorField(dimensions=3)
    other_embedding = VectorField(dimensions=3, null=True)

    class Meta:
        table = "vec_entry"
