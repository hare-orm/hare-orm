from __future__ import annotations

from decimal import Decimal

from hare.fields.validators.max_digits_validator import MaxDigitsValidator
from hare.fields.validators.max_length_validator import MaxLengthValidator
from hare.fields.validators.max_value_validator import MaxValueValidator
from hare.fields.validators.min_length_validator import MinLengthValidator
from hare.fields.validators.min_value_validator import MinValueValidator
from hare.models.constants import IMMUTABLE_FIELD_VALUE_TYPES, PLACEHOLDER_FIELD_VALUE_TYPES
from hare.query.rows.enums import WriteCodecKind

#: The field types whose Min/MaxValueValidator a field codec checks itself.
INLINE_VALUE_CHECK_TYPES = frozenset({int, float, Decimal})
#: The values a dirty-field baseline keeps as they are - ``FieldSnapshot.copy_value()`` deep-copies
#: any other.
SNAPSHOT_KEPT_VALUE_TYPES = IMMUTABLE_FIELD_VALUE_TYPES + PLACEHOLDER_FIELD_VALUE_TYPES
#: The validator classes a field codec checks itself.
INLINE_CHECK_VALIDATOR_TYPES: frozenset[type] = frozenset(
    {MinValueValidator, MaxValueValidator, MinLengthValidator, MaxLengthValidator, MaxDigitsValidator}
)

#: The write codecs whose value depends on the time zone settings of the moment - a filter value
#: of one is converted by its field.
TEMPORAL_WRITE_CODEC_KINDS = frozenset({WriteCodecKind.DATETIME, WriteCodecKind.DATE, WriteCodecKind.TIME})
