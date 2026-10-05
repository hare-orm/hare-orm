"""Two model pairs sharing table names, one pair in its own Postgres schema."""

from hare import fields
from hare.models import Model


class OtherSchemaWriter(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "same_name_writer"
        schema = "hare_same_name_other"


class OtherSchemaNote(Model):
    id = fields.IntField(primary_key=True)
    writer: fields.ForeignKeyRelation[OtherSchemaWriter] = fields.ForeignKeyField(
        "models.OtherSchemaWriter", related_name="notes"
    )
    readers: fields.ManyToManyRelation[OtherSchemaWriter] = fields.ManyToManyField(
        "models.OtherSchemaWriter", related_name="read_notes", through="same_name_note_reader"
    )

    class Meta:
        table = "same_name_note"
        schema = "hare_same_name_other"


class PublicWriter(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "same_name_writer"


class PublicNote(Model):
    id = fields.IntField(primary_key=True)
    writer: fields.ForeignKeyRelation[PublicWriter] = fields.ForeignKeyField(
        "models.PublicWriter", related_name="notes"
    )
    readers: fields.ManyToManyRelation[PublicWriter] = fields.ManyToManyField(
        "models.PublicWriter", related_name="read_notes", through="same_name_note_reader"
    )

    class Meta:
        table = "same_name_note"
