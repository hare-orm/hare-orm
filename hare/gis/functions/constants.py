from __future__ import annotations

from typing import Any

from hare.fields.data.boolean_field import BooleanField
from hare.fields.data.json.json_field import JSONField
from hare.fields.data.numeric.float_field import FloatField
from hare.fields.data.numeric.int_field import IntField
from hare.fields.data.text import TextField

#: Reusable fields for decoding the spatial functions' results.
MEASURE_FIELD = FloatField()
COUNT_FIELD = IntField()
FLAG_FIELD = BooleanField()
TEXT_FIELD = TextField()
GEO_JSON_FIELD: JSONField[Any] = JSONField()
