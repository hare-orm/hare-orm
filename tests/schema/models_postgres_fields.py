from hare import Model, fields
from hare.dialects.postgresql.fields.array import ArrayField
from hare.dialects.postgresql.fields.search import TSVectorField


class PostgresFields(Model):
    tsvector = TSVectorField()
    text_array = ArrayField(base_field=fields.TextField(), default=["a", "b", "c"])
    varchar_array = ArrayField(base_field=fields.CharField(max_length=32), default=["aa", "bbb", "cccc"])
    int_array = ArrayField(base_field=fields.IntField(), default=[1, 2, 3], null=True)
    real_array = ArrayField(
        base_field=fields.FloatField(),
        default=[1.1, 2.2, 3.3],
        description="this is array of real numbers",
    )

    class Meta:
        table = "postgres_fields"
