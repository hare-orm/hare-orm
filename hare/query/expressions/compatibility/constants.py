from __future__ import annotations

from typing import Any

from hare.fields.data.numeric.decimal_field import DecimalField
from hare.fields.data.numeric.float_field import FloatField
from hare.fields.data.numeric.int_field import IntField
from hare.fields.field import Field

#: Same as COALESCE_NUMERIC_FIELD_LITERAL_TYPES, for a default value that resolves to another
#: field (e.g. `F("other_column")`) instead of a bare literal.
COALESCE_NUMERIC_FIELD_DEFAULT_FIELD_CLASSES: dict[type[Field[Any]], tuple[type[Field[Any]], ...]] = {
    IntField: (IntField,),
    FloatField: (IntField, FloatField, DecimalField),
    DecimalField: (IntField, DecimalField),
}
