"""The lookups of the value at a JSON path (``F("data__key")``, a ``JSONPathField``)."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.core.cache import Cache
from hare.exceptions import QueryError, UnSupportedError
from hare.fields import Field, JSONField
from hare.fields.data.json.json_path_field import JSONPathField
from hare.query.enums import Lookup
from hare.query.filters.constants import (
    JSON_PATH_CONTAINER_LOOKUPS,
    JSON_PATH_LIST_LOOKUPS,
    JSON_PATH_TEXT_LOOKUPS,
    JSON_PATH_VALUE_LOOKUPS,
)
from hare.query.filters.encoders import ValueEncoders
from hare.query.filters.field_lookup import FieldLookup
from hare.query.filters.lookups import Lookups
from hare.sql.terms.base.term import Term
from hare.sql.terms.criteria.criterion import Criterion

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect


class JsonPathLookups:
    """Lookups of a JSON path value: comparisons match JSON values, the text lookups match the
    value's text."""

    #: The field a list of path values is bound as.
    ELEMENT_FIELD: ClassVar[Field[Any]] = JSONField()

    @staticmethod
    def greater_than(field: Term, value: Any) -> Criterion:
        """The JSON value is greater, in ``jsonb`` order."""
        return field > value

    @staticmethod
    def greater_equal(field: Term, value: Any) -> Criterion:
        """The JSON value is greater or equal, in ``jsonb`` order."""
        return field >= value

    @staticmethod
    def less_than(field: Term, value: Any) -> Criterion:
        """The JSON value is less, in ``jsonb`` order."""
        return field < value

    @staticmethod
    def less_equal(field: Term, value: Any) -> Criterion:
        """The JSON value is less or equal, in ``jsonb`` order."""
        return field <= value

    @staticmethod
    def between(field: Term, value: list[Any]) -> Criterion:
        """The JSON value is between the two bounds, in ``jsonb`` order."""
        lower, upper = value
        return Lookups.between(field, (lower, upper))

    @staticmethod
    def get_path_field(field: Field[Any]) -> JSONPathField:
        """The JSON path field comparing values of a JSON path or a whole JSON field."""
        return field if isinstance(field, JSONPathField) else cast("JSONField[Any]", field).get_path_value_field()

    @classmethod
    def encode_value(cls, value: Any, instance: Any, field: Field[Any], dialect: Dialect) -> Any:
        """Encodes one filter value as a JSON value.

        Raises:
            ValidationError: The value isn't JSON serializable.
        """
        return cls.get_path_field(field).encode_comparison_value(value, dialect)

    @classmethod
    def encode_values(cls, values: Any, instance: Any, field: Field[Any], dialect: Dialect) -> list[Any]:
        """Encodes an ``in``/``not_in``/``range`` list, each value as a JSON value.

        Raises:
            UnSupportedError: ``values`` isn't a list/tuple/set.
            ValidationError: A value isn't JSON serializable.
        """
        if not isinstance(values, (list, tuple, set)):
            raise UnSupportedError(
                f"{field.model_field_name}: expected a list/tuple/set of values for this lookup, got {values!r}"
            )
        path_field = cls.get_path_field(field)
        return [path_field.encode_comparison_value(value, dialect, as_parameter=True) for value in values]

    @staticmethod
    def encode_text_value(value: Any, instance: Any, field: Field[Any], dialect: Dialect) -> Any:
        """Encodes the value of a text lookup (``contains``, ``istartswith``, ...), which matches the
        value's text.

        Raises:
            QueryError: ``value`` is a JSON object/array.
            UnSupportedError: ``value`` is ``None``.
        """
        if isinstance(value, (dict, list, tuple)):
            raise QueryError(
                f"{field.model_field_name}: a text lookup of a JSON path matches the value's text and takes a "
                f"string, got {value!r} - test JSON containment with the whole field's __contains"
            )
        return ValueEncoders.encode_string(value, instance, field, dialect)

    @classmethod
    def get_ordering_operators(cls) -> dict[str, Callable[..., Criterion]]:
        """The operator of each lookup ordering JSON values."""
        return {
            "gt": cls.greater_than,
            "gte": cls.greater_equal,
            "lt": cls.less_than,
            "lte": cls.less_equal,
            "range": cls.between,
        }

    @classmethod
    def get_ordering_lookups(cls) -> dict[str, FieldLookup]:
        """The lookups ordering JSON values - of a JSON path or a whole JSON field.

        Returns:
            The ``gt``/``gte``/``lt``/``lte``/``range`` lookups.
        """
        return {
            suffix: FieldLookup(ordering_operator, cls.encode_values, array_element_field=cls.ELEMENT_FIELD)  # type: ignore[arg-type]
            if suffix in JSON_PATH_LIST_LOOKUPS
            else FieldLookup(ordering_operator, cls.encode_value)
            for suffix, ordering_operator in cls.get_ordering_operators().items()
        }

    #: (class,) -> every lookup of a JSON path value - built from the registries.
    LOOKUPS: ClassVar[Cache[dict[str, FieldLookup]]] = Cache(1)

    @classmethod
    def get_lookups(cls) -> dict[str, FieldLookup]:
        """Every lookup of a JSON path value.

        Returns:
            The lookups by suffix.
        """
        lookups = cls.LOOKUPS.get((cls,))
        if lookups is None:
            lookups = cls.LOOKUPS[(cls,)] = cls.build_lookups()
        return lookups

    @classmethod
    def build_lookups(cls) -> dict[str, FieldLookup]:
        """The lookups ``get_lookups()`` keeps."""
        # Local import: the field lookups import this module's field module.
        from hare.query.filters.field_lookups import FieldLookups

        lookups = cls.get_ordering_lookups()
        for suffix, field_lookup in FieldLookups.get_generic(None).items():
            if suffix in lookups:
                continue
            if suffix in JSON_PATH_VALUE_LOOKUPS:
                lookups[suffix] = field_lookup.with_changes(value_encoder=cls.encode_value)
            elif suffix in JSON_PATH_LIST_LOOKUPS:
                lookups[suffix] = field_lookup.with_changes(
                    value_encoder=cls.encode_values,
                    array_element_field=cls.ELEMENT_FIELD,  # type: ignore[arg-type]
                )
            elif suffix in JSON_PATH_TEXT_LOOKUPS:
                lookups[suffix] = field_lookup.with_changes(
                    value_encoder=cls.encode_text_value, compares_json_path_text=True
                )
            elif suffix in (Lookup.ISNULL, Lookup.NOT_ISNULL):
                lookups[suffix] = field_lookup
        json_lookups = FieldLookups.get_json(cast("JSONField[Any]", cls.ELEMENT_FIELD))  # type: ignore[arg-type]
        for suffix in JSON_PATH_CONTAINER_LOOKUPS:
            lookups[suffix] = json_lookups[suffix]
        return lookups

    @classmethod
    def get_lookup_names(cls) -> frozenset[str]:
        """The suffixes of the lookups of a JSON path value."""
        return frozenset(suffix for suffix in cls.get_lookups() if suffix)
