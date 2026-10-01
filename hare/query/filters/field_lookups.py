"""The lookups of every field: each ``__<lookup>`` suffix a field's value takes, built once per field."""

from __future__ import annotations

import operator
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar

from hare.core.cache import Cache
from hare.fields.base.field import Field
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.generated import GeneratedField
from hare.query.enums import LookupValueShape
from hare.query.filters.annotation_container_lookups import AnnotationContainerLookups
from hare.query.filters.constants import DATETIME_DATE_PART_LOOKUPS
from hare.query.filters.date_part_lookups import DatePartLookups
from hare.query.filters.encoders import ValueEncoders
from hare.query.filters.field_lookup import FieldLookup
from hare.query.filters.json_lookups import JsonLookups
from hare.query.filters.lookups import Lookups

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.data.json.json_field import JSONField
    from hare.fields.registered_lookup import RegisteredLookup
    from hare.fields.relations.fields.backward_fk_relation import BackwardFKRelation
    from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance


class FieldLookups:
    """The lookups of a field's value by suffix (``""`` is plain equality): the field's own set
    (``Field.get_lookups()``) with the ``register_lookup()`` lookups of its class and base classes
    added - a built-in suffix always wins. A value with no field (an annotation whose type isn't
    known) takes the generic set, every date part and the lookups registered on ``Field`` itself."""

    #: The built lookups. A field's are kept on the field itself (``Field.built_lookups``), those
    #: of a value with no field in ``field_blind_lookups`` - each with the cache's generation it was
    #: built in. A relation's lookups read the related model's key, so a change to any model drops
    #: them all.
    built_lookups: ClassVar[Cache[Any]] = Cache(holds_sql=False, keyed_by_model=False, depends_on_other_models=True)
    #: The built lookups of a value with no field, with the generation they were built in.
    field_blind_lookups: ClassVar[tuple[int, dict[str, FieldLookup]] | None] = None

    @classmethod
    def get(cls, field: Field[Any] | None) -> dict[str, FieldLookup]:
        """The lookups of a field's value - a GeneratedField's are those of its ``output_field``.

        Args:
            field: The field, None for a value with no field.

        Returns:
            The lookups by suffix.
        """
        generation = cls.built_lookups.generation
        if field is None:
            blind_lookups = cls.field_blind_lookups
            if blind_lookups is not None and blind_lookups[0] == generation:
                return blind_lookups[1]
            lookups = cls.get_with_registered(None, cls.get_generic(None))
            cls.field_blind_lookups = (generation, lookups)
            return lookups
        # Kept on the field itself: a lookup refers back to its field (the field an __in list
        # binds as), which an entry keyed by the field would keep alive forever - and a field
        # can't be referred to weakly.
        built_lookups = field.built_lookups
        if built_lookups is not None and built_lookups[0] == generation:
            return built_lookups[1]
        effective_field = cls.get_effective_field(field)
        lookups = cls.get_with_registered(effective_field, effective_field.get_lookups())
        field.built_lookups = (generation, lookups)
        return lookups

    @staticmethod
    def get_effective_field(field: Field[Any]) -> Field[Any]:
        """A field, or the field a GeneratedField computes."""
        return field.output_field if isinstance(field, GeneratedField) else field

    @classmethod
    def is_supported(cls, field: Field[Any] | None, suffix: str) -> bool:
        """Whether a lookup is one of the field's ``supported_lookups``.

        Args:
            field: The field, None for a value with no field.
            suffix: The lookup's suffix.

        Returns:
            True unless the field limits its lookups and leaves this one out.
        """
        if field is None:
            return True
        supported_lookups = cls.get_effective_field(field).supported_lookups
        return supported_lookups is None or suffix in supported_lookups

    @classmethod
    def get_with_registered(cls, field: Field[Any] | None, lookups: dict[str, FieldLookup]) -> dict[str, FieldLookup]:
        """Adds the ``register_lookup()`` lookups of the field's class and its base classes.

        Args:
            field: The field, None for a value with no field.
            lookups: The field's own lookups.

        Returns:
            The lookups, the built-in ones kept where a registered one has the same suffix.
        """
        field_classes = type(field).__mro__ if field is not None else (Field,)
        lookups = dict(lookups)
        for field_class in field_classes:
            for suffix, registered_lookup in field_class.__dict__.get("registered_lookups", {}).items():
                if suffix not in lookups:
                    lookups[suffix] = cls.get_registered(registered_lookup, field)
        return lookups

    @staticmethod
    def get_registered(registered_lookup: RegisteredLookup, field: Field[Any] | None) -> FieldLookup:
        """Builds one ``register_lookup()`` lookup for a field - a lookup taking a list or a range
        with no value encoder of its own has each item converted by the field.

        Args:
            registered_lookup: The lookup.
            field: The field, None for a value with no field.

        Returns:
            The lookup.
        """
        field_lookup = registered_lookup.builder(field)
        if field_lookup.value_encoder is None and registered_lookup.value_shape != LookupValueShape.VALUE:
            return field_lookup.with_changes(value_encoder=ValueEncoders.encode_list)
        return field_lookup

    @staticmethod
    def get_generic(field: Field[Any] | None) -> dict[str, FieldLookup]:
        """The lookups every scalar value has - equality, comparisons, membership, ``isnull``,
        ``range``, the text and pattern lookups; a value with no field also takes every date part
        and the array/range/JSON lookups, rejected once its value turns out to be none of those.

        Args:
            field: The field, None for a value with no field.

        Returns:
            The lookups by suffix.
        """
        text_function = None if field is None else field.get_like_text_function()

        def get_text_lookup(text_operator: Any) -> FieldLookup:
            if text_function is not None:
                text_operator = partial(text_operator, text_function=text_function)
            return FieldLookup(text_operator, ValueEncoders.encode_string)

        lookups = {
            "": FieldLookup(operator.eq),
            "not": FieldLookup(Lookups.not_equal),
            "in": FieldLookup(Lookups.is_in, ValueEncoders.encode_list, array_element_field=field),
            "not_in": FieldLookup(Lookups.not_in, ValueEncoders.encode_list, array_element_field=field),
            "isnull": FieldLookup(Lookups.is_null, ValueEncoders.encode_bool),
            "not_isnull": FieldLookup(Lookups.not_null, ValueEncoders.encode_bool),
            "gte": FieldLookup(operator.ge),
            "lte": FieldLookup(operator.le),
            "gt": FieldLookup(operator.gt),
            "lt": FieldLookup(operator.lt),
            "range": FieldLookup(Lookups.between, ValueEncoders.encode_list),
            "contains": get_text_lookup(Lookups.contains),
            "startswith": get_text_lookup(Lookups.starts_with),
            "search": FieldLookup(Lookups.search, ValueEncoders.encode_string),
            "endswith": get_text_lookup(Lookups.ends_with),
            "iexact": get_text_lookup(Lookups.insensitive_exact),
            "icontains": get_text_lookup(Lookups.insensitive_contains),
            "istartswith": get_text_lookup(Lookups.insensitive_starts_with),
            "iendswith": get_text_lookup(Lookups.insensitive_ends_with),
            "posix_regex": FieldLookup(Lookups.posix_regex, ValueEncoders.encode_string, text_function=text_function),
            "iposix_regex": FieldLookup(
                Lookups.insensitive_posix_regex, ValueEncoders.encode_string, text_function=text_function
            ),
        }
        if field is None:
            lookups.update(DatePartLookups.get_lookups(DATETIME_DATE_PART_LOOKUPS, is_datetime_field=False))
            lookups.update(AnnotationContainerLookups.get_lookups())
        return lookups

    @classmethod
    def get_date_parts(cls, field: Field[Any], date_part_lookups: dict[str, Any]) -> dict[str, FieldLookup]:
        """The generic lookups of a date/time field with its date parts (``__year``, ``__hour``, ...).

        Args:
            field: The field.
            date_part_lookups: The suffix of each date part the field's value has.

        Returns:
            The lookups by suffix.
        """
        return {
            **cls.get_generic(field),
            **DatePartLookups.get_lookups(date_part_lookups, is_datetime_field=isinstance(field, DatetimeField)),
        }

    @staticmethod
    def get_json(field: JSONField[Any]) -> dict[str, FieldLookup]:
        """The lookups of a whole JSON value - equality, membership, containment, keys and the
        ordering of JSON values.

        Args:
            field: The JSON field.

        Returns:
            The lookups by suffix.
        """
        # Local import: the JSON path lookups read the JSON field module.
        from hare.query.filters.json_path_lookups import JsonPathLookups

        return {
            "": FieldLookup(JsonLookups.equal),
            "not": FieldLookup(JsonLookups.not_equal),
            "in": FieldLookup(JsonLookups.is_in, ValueEncoders.encode_list, array_element_field=field),
            "not_in": FieldLookup(JsonLookups.not_in, ValueEncoders.encode_list, array_element_field=field),
            "isnull": FieldLookup(Lookups.is_null, ValueEncoders.encode_bool),
            "not_isnull": FieldLookup(Lookups.not_null, ValueEncoders.encode_bool),
            "contains": FieldLookup(JsonLookups.contains),
            "contained_by": FieldLookup(JsonLookups.contained_by),
            "has_key": FieldLookup(JsonLookups.has_key, ValueEncoders.encode_string),
            "has_keys": FieldLookup(JsonLookups.has_keys, ValueEncoders.encode_string_list),
            "has_any_keys": FieldLookup(JsonLookups.has_any_keys, ValueEncoders.encode_string_list),
            "filter": FieldLookup(JsonLookups.filter, ValueEncoders.encode_json),
            **JsonPathLookups.get_ordering_lookups(),
        }

    @staticmethod
    def get_many_to_many(field: ManyToManyFieldInstance[Any]) -> dict[str, FieldLookup]:
        """The lookups of a many-to-many relation itself - the related rows' keys, compared on the
        through table (a row of key columns for a composite primary key).

        Args:
            field: The relation.

        Returns:
            The lookups by suffix.
        """
        target_meta = field.related_model._meta
        if target_meta.pk is None:
            target_pk_fields = tuple(target_meta.pk_fields)
            return {
                "": FieldLookup(Lookups.row_equal, ValueEncoders.encode_composite_pk),
                "not": FieldLookup(Lookups.row_not_equal, ValueEncoders.encode_composite_pk),
                "in": FieldLookup(
                    Lookups.row_is_in,
                    ValueEncoders.encode_composite_related_list,
                    array_element_fields=target_pk_fields,
                ),
                "not_in": FieldLookup(
                    Lookups.row_not_in,
                    ValueEncoders.encode_composite_related_list,
                    array_element_fields=target_pk_fields,
                ),
            }
        target_pk = target_meta.pk
        return {
            "": FieldLookup(operator.eq, ValueEncoders.encode_related_value),
            "not": FieldLookup(Lookups.not_equal, ValueEncoders.encode_related_value),
            "in": FieldLookup(Lookups.is_in, ValueEncoders.encode_related_list, array_element_field=target_pk),
            "not_in": FieldLookup(Lookups.not_in, ValueEncoders.encode_related_list, array_element_field=target_pk),
        }

    @staticmethod
    def get_backward(field: BackwardFKRelation[Any]) -> dict[str, FieldLookup]:
        """The lookups of a backward FK/O2O relation itself - ``isnull``, and the related rows'
        primary key where the related model has a single-column one.

        Args:
            field: The relation.

        Returns:
            The lookups by suffix.
        """
        lookups = {
            "isnull": FieldLookup(Lookups.is_null),
            "not_isnull": FieldLookup(Lookups.not_null),
        }
        target_pk = field.related_model._meta.pk
        if target_pk is None:
            return lookups
        return {
            **lookups,
            "": FieldLookup(operator.eq, ValueEncoders.encode_related_value),
            "not": FieldLookup(Lookups.not_equal, ValueEncoders.encode_related_value),
            "in": FieldLookup(Lookups.is_in, ValueEncoders.encode_related_list, array_element_field=target_pk),
            "not_in": FieldLookup(Lookups.not_in, ValueEncoders.encode_related_list, array_element_field=target_pk),
        }
