from dataclasses import dataclass
from typing import Any

from hare import fields


@dataclass
class Document:
    flag: Any


class ContainerDirtySamples:
    """Document fields nested in the supported column shapes."""

    @staticmethod
    def get_sample(shape, typed):
        field = fields.JSONTextField(field_type=Document if typed else None)
        document = Document(1) if typed else {"flag": 1}
        if shape == "tuple":
            return fields.TupleField([field]), (document,), (0,)
        if shape == "nested-tuple":
            return fields.TupleField([fields.TupleField([field])]), ((document,),), (0, 0)
        if shape == "named":
            return fields.TupleField({"document": field}), {"document": document}, ("document",)
        if shape == "array":
            return fields.ArrayField(field), [document], (0,)
        if shape == "map":
            return fields.MapField(fields.TextField(), field), {"document": document}, ("document",)
        if shape == "nested":
            return fields.NestedField({"document": field}), [{"document": document}], (0, "document")
        return field, document, ()

    @staticmethod
    def get_document(value, path):
        for key in path:
            value = value[key]
        return value
