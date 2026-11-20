from __future__ import annotations

from typing import Any

from hare.exceptions import FieldError, QueryError, UnSupportedError
from tests.contracts.constants import GENERATED_FIELD_OPERATIONS
from tests.contracts.field_sample import FieldSample
from tests.contracts.generated_field_samples import (
    GENERATED_INTEGER_SAMPLES,
    GENERATED_JSON_OBJECT_VALUES,
    GENERATED_OPERATION_SAMPLES,
    GENERATED_VALUE_OPERATIONS,
)


class GeneratedFieldExpectations:
    """Independent expectations for selected field/operation/profile combinations.

    Every field is checked through value-reading operations. Query operations have an
    explicit sample inventory: unsupported SQL combinations are never discovered by
    treating the implementation's exception as its expected result.
    """

    @staticmethod
    def get_operations(sample: FieldSample) -> tuple[str, ...]:
        return tuple(
            operation
            for operation in GENERATED_FIELD_OPERATIONS
            if operation in GENERATED_VALUE_OPERATIONS or sample.name in GENERATED_OPERATION_SAMPLES[operation]
        )

    @staticmethod
    def get_error(sample: FieldSample, operation: str, dialect: str) -> tuple[type[Exception], str] | None:
        if operation == "lag_fractional" and sample.name in GENERATED_INTEGER_SAMPLES | {"decimal"}:
            return QueryError, r"Lag\(.+\) default 0\.5 doesn't fit the field's own type"
        if operation == "coalesce_fractional" and sample.name in {"boolean", "char"}:
            return FieldError, r"Coalesce\(\).*don't make one type"
        if operation in {"aggregate", "aggregate_empty"} and sample.name == "boolean" and dialect == "postgresql":
            # PostgreSQL provides bool_and/bool_or, not MIN/MAX(boolean).
            return UnSupportedError, r"Min\(\) of a BooleanField.*has no MIN"
        return None

    @staticmethod
    def get_json_values(sample: FieldSample) -> list[Any]:
        return list(sample.json_values or sample.values)

    @staticmethod
    def get_json_objects(sample: FieldSample, dialect: str) -> list[dict[str, Any]]:
        """The documented SQL JSON forms of the selected samples, without a field serializer."""
        values = GENERATED_JSON_OBJECT_VALUES[sample.name]
        if sample.name == "boolean" and dialect == "sqlite":
            values = (1, 0)
        elif sample.name == "binary" and dialect == "postgresql":
            values = ("\\x0068617265",)
        return [{"item": value} for value in values]

    @staticmethod
    def get_ordered_ids(sample: FieldSample, dialect: str) -> list[int]:
        """Python-orderable samples retain the profile's documented default NULL placement."""
        nulls_first = dialect == "sqlite"
        return [
            number
            for number, _ in sorted(
                enumerate(sample.values, 1),
                key=lambda pair: (pair[1] is not None if nulls_first else pair[1] is None, pair[1]),
            )
        ]

    @staticmethod
    def get_extrema(sample: FieldSample) -> dict[str, Any]:
        pairs = [
            (value, json_value)
            for value, json_value in zip(
                sample.values, GeneratedFieldExpectations.get_json_values(sample), strict=True
            )
            if value is not None
        ]
        if not pairs:
            return {"minimum": None, "maximum": None}
        return {
            "minimum": min(pairs, key=lambda pair: pair[0])[1],
            "maximum": max(pairs, key=lambda pair: pair[0])[1],
        }
