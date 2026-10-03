from hare import Model, fields
from hare.dialects.postgresql.fields.array import ArrayField
from hare.dialects.postgresql.fields.citext import CitextField


class CitextContact(Model):
    id = fields.IntField(primary_key=True)
    email = CitextField()
    name = fields.CharField(max_length=100, null=True)

    class Meta:
        table = "citext_contact"


class CitextTagged(Model):
    id = fields.IntField(primary_key=True)
    tags = ArrayField(base_field=CitextField(), null=True)

    class Meta:
        table = "citext_tagged"
