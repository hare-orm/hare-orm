from hare import Model, fields
from hare.ddl.constraints.unique_constraint import UniqueConstraint


class UpperName(Model):
    id = fields.IntField(primary_key=True)
    first = fields.CharField(max_length=10, source_field="É")
    second = fields.CharField(max_length=10, source_field="é")

    class Meta:
        table = "Таблица"
        constraints = (
            UniqueConstraint(fields=("first",), name="Índice"),
            UniqueConstraint(fields=("second",), name="índice"),
        )


class LowerName(Model):
    id = fields.IntField(primary_key=True)
    value = fields.IntField()

    class Meta:
        table = "таблица"
