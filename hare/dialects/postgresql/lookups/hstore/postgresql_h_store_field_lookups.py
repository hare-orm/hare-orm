from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.core.caching.cache import Cache
from hare.dialects.postgresql.fields.hstore.h_store_field import HStoreField
from hare.fields.data.containers.array_field import ArrayField
from hare.fields.data.text.text_field import TextField

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
    from hare.query.filters.lookups.field_lookup import FieldLookup
from hare.dialects.postgresql.lookups.hstore.postgresql_h_store_lookups import PostgresqlHStoreLookups


class PostgresqlHStoreFieldLookups:
    """The lookups of an ``HStoreField``: equality and membership of the whole value, ``isnull``,
    ``contains``/``contained_by`` of pairs, and ``has_key``/``has_keys``/``has_any_keys``."""

    #: The whole-value lookups kept from the generic set.
    GENERIC_LOOKUPS: ClassVar[frozenset[str]] = frozenset({"", "not", "in", "not_in", "isnull", "not_isnull"})
    #: Shared output and element fields - the statement plans hold output fields weakly.
    #: The field a list of whole values is bound as.
    ELEMENT_FIELD: ClassVar[Field[Any]] = HStoreField()
    #: The field of one key's value (``attributes__color``).
    VALUE_FIELD: ClassVar[Field[Any]] = TextField()
    #: The field of every key or value as a text array (``attributes__keys``).
    TEXT_ARRAY_FIELD: ClassVar[Field[Any]] = ArrayField(TextField())

    #: (class,) -> every lookup of an hstore field - built from the registries.
    LOOKUPS: ClassVar[Cache[dict[str, FieldLookup]]] = Cache(1)

    @classmethod
    def get_lookups(cls) -> dict[str, FieldLookup]:
        """Builds every lookup of an hstore field.

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
        # Local import: the filters package imports the dialects package.
        from hare.query.filters import ValueEncoders
        from hare.query.filters.lookups.field_lookup import FieldLookup
        from hare.query.filters.lookups.field_lookups import FieldLookups

        lookups: dict[str, FieldLookup] = {}
        for suffix, field_lookup in FieldLookups.get_generic(None).items():
            if suffix in cls.GENERIC_LOOKUPS:
                lookups[suffix] = (
                    field_lookup.with_changes(array_element_field=cls.ELEMENT_FIELD)  # type: ignore[arg-type]
                    if field_lookup.value_encoder is ValueEncoders.encode_list
                    else field_lookup
                )
        for lookup_name, hstore_operator, value_encoder in (
            ("contains", PostgresqlHStoreLookups.contains, None),
            ("contained_by", PostgresqlHStoreLookups.contained_by, None),
            ("has_key", PostgresqlHStoreLookups.has_key, ValueEncoders.encode_string),
            ("has_keys", PostgresqlHStoreLookups.has_keys, ValueEncoders.encode_string_list),
            ("has_any_keys", PostgresqlHStoreLookups.has_any_keys, ValueEncoders.encode_string_list),
        ):
            lookups[lookup_name] = FieldLookup(hstore_operator, value_encoder)
        return lookups

    @classmethod
    def get_lookup_names(cls) -> frozenset[str]:
        """The names of the lookups of an hstore field - a path segment that is one of them is the
        lookup, any other names a key."""
        return frozenset(suffix for suffix in cls.get_lookups() if suffix)
