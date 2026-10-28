from pydantic import BaseModel

from hare import Model, fields


class JsonbDocument(BaseModel):
    title: str


class JsonbDocumentRow(Model):
    id = fields.IntField(primary_key=True)
    document = fields.JSONField(field_type=JsonbDocument, null=True)
