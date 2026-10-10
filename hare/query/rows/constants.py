from __future__ import annotations

from decimal import Decimal

from hare.fields.validators.limits.max_digits_validator import MaxDigitsValidator
from hare.fields.validators.limits.max_length_validator import MaxLengthValidator
from hare.fields.validators.limits.max_value_validator import MaxValueValidator
from hare.fields.validators.limits.min_length_validator import MinLengthValidator
from hare.fields.validators.limits.min_value_validator import MinValueValidator
from hare.models.constants import IMMUTABLE_FIELD_VALUE_TYPES, PLACEHOLDER_FIELD_VALUE_TYPES
from hare.query.rows.enums import WriteCodecType

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
TEMPORAL_WRITE_CODEC_TYPES = frozenset({WriteCodecType.DATETIME, WriteCodecType.DATE, WriteCodecType.TIME})

#: Separates a ``values()`` output key from the index of one column of a composite key it selects
#: - ``values(target=...)`` over a composite foreign key selects ``target__hare_key_component_0``,
#: ``..._1`` and returns them combined as one tuple under ``target``.
COMPOSITE_KEY_COMPONENT_ALIAS_SEPARATOR = "__hare_key_component_"

#: How many namedtuple classes of ``values_list(named=True)`` selections are kept.
ROW_CLASS_CACHE_SIZE = 256
