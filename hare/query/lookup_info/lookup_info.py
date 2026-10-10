from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from hare.fields.field import Field
from hare.query.enums import Lookup, LookupTarget, LookupValueShape
from hare.query.filters.lookups.field_lookup import FieldLookup
from hare.query.filters.lookups.field_transforms import TermTransform

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.fields.relations.fields.generic_foreign_key_field_instance import GenericForeignKeyFieldInstance
    from hare.models import Model


@dataclasses.dataclass(frozen=True, slots=True)
class LookupInfo:
    """What one ``.filter()`` key compares, and the value it takes.

    ``author__name__icontains`` crosses ``author``, compares ``Author.name`` with ``icontains``
    and takes one string; ``tags__in`` crosses the ``tags`` relation and takes a list of tag
    keys; ``created__year__gte`` reads the year of ``created`` and takes one integer.

    Attributes:
        key: The filter key.
        model: The model the key starts at.
        relations: The relations the key crosses, in order - forward FK/O2O, reverse FK/O2O and
            many-to-many fields. A lookup on a relation itself (``author=``, ``tags__in=``)
            crosses it and compares the related model's key.
        field: The field the value is compared with - a tuple of fields for a composite key
            (``pk`` of a composite primary key, a relation to one), None for an annotation whose
            type is only known once the query runs.
        transforms: The path read inside the field's value before the lookup - a date part
            (``year``), a datetime's ``date``/``time``, a JSON key path, an array index, slice or
            ``len``, a range bound or flag, ``unaccent``, an hstore key.
        lookup: The lookup - a ``Lookup``, or the name of a lookup registered with
            ``Field.register_lookup()``.
        value_shape: Whether the filter value is one value, a list or a two-item range.
        value_type: The type of the value - of each item of a list or range. A tuple of types for
            a composite key; ``bool`` for ``isnull``; ``int`` for a date part or ``len``; ``str``
            for a text lookup or a JSON/hstore key; ``object`` for any JSON value.
        crosses_to_many: Whether a relation the key crosses holds many rows for one row
            (a reverse FK or a many-to-many relation) - the filter joins it.
        requires_extension: The database extension the lookup needs (``pg_trgm`` for a trigram
            lookup, ``unaccent``), None for none.
        dialects: The names of the dialects the field and the lookup exist on, None for every
            dialect - or for a field taking its column type from the dialect, which
            ``is_supported()`` checks against the dialect asked about. ``is_supported()`` also asks
            the dialect whether it implements the lookup.
    """

    key: str
    model: type[Model]
    relations: tuple[Field[Any], ...]
    field: Field[Any] | tuple[Field[Any], ...] | None
    transforms: tuple[str, ...]
    lookup: Lookup | str
    value_shape: LookupValueShape
    value_type: Any
    crosses_to_many: bool
    requires_extension: str | None
    dialects: frozenset[str] | None
    #: What the key compares at the end of its relations.
    target: LookupTarget = dataclasses.field(default=LookupTarget.FIELD, compare=False, repr=False)
    #: The lookup the dialect runs - None where a composite key or a relation's emptiness is
    #: compared without one.
    field_lookup: FieldLookup | None = dataclasses.field(default=None, compare=False, repr=False)
    #: The field of the value the lookup compares, after the transforms.
    value_field: Field[Any] | None = dataclasses.field(default=None, compare=False, repr=False)
    #: The functions building the term each transform reads (a JSON path has none - it is read
    #: as ``F()`` of the path).
    term_transforms: tuple[TermTransform, ...] = dataclasses.field(default=(), compare=False, repr=False)
    #: The generic foreign key a ``GENERIC_RELATION`` key compares - its branches are compared
    #: with plain lookups every dialect runs.
    generic_field: GenericForeignKeyFieldInstance[Any] | None = dataclasses.field(
        default=None, compare=False, repr=False
    )
    #: Whether a ``GENERIC_RELATION`` key compares the generic foreign key's type.
    compares_generic_type: bool = dataclasses.field(default=False, compare=False, repr=False)

    def is_supported(self, dialect: Dialect) -> bool:
        """Whether a query on ``dialect`` can run this lookup.

        Args:
            dialect: The dialect.

        Returns:
            True when the field exists on the dialect, the dialect implements the lookup, and
            the dialect has extensions where the lookup needs one.
        """
        if self.generic_field is not None:
            return True
        return dialect.filter_operators.supports_lookup(self)
