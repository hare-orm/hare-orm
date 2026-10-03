from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.queryset.calls_before_setup import CallsBeforeSetup


class QueryOptions:
    """The settings most queries leave alone, kept in one object instead of a slot each. The object is
    shared with clones and never changed: assigning a setting gives the query a new object. A query
    that set none holds ``DEFAULT``. Collections are stored immutable.
    """

    #: Each setting's value when unset. Every one is falsy - ``is_default()`` is a truth test.
    DEFAULTS: ClassVar[dict[str, Any]] = {
        "select_for_update": False,
        "select_for_update_nowait": False,
        "select_for_update_skip_locked": False,
        "select_for_update_of": frozenset(),
        "select_for_update_no_key": False,
        "cursor_values": (),
        "before_cursor_values": (),
        "reverse_result_order": False,
        "with_ctes": (),
        "extension_calls": (),
        "select_related_extra_conditions": MappingProxyType({}),
        "distinct_on": (),
        "group_bys": (),
        "alias_keys": frozenset(),
        "does_not_exist_exception": None,
        "is_single_row_of_slice": False,
        "default_ordering_disabled": False,
        "fields_for_select": (),
        "deferred_fields": (),
        "deferred_related_fields": frozenset(),
        "explicitly_select_related": frozenset(),
        "calls_before_setup": None,
    }

    select_for_update: bool
    select_for_update_nowait: bool
    select_for_update_skip_locked: bool
    select_for_update_of: frozenset[str]
    select_for_update_no_key: bool
    cursor_values: tuple[Any, ...]
    before_cursor_values: tuple[Any, ...]
    reverse_result_order: bool
    with_ctes: tuple[tuple[str, Any], ...]
    extension_calls: tuple[Any, ...]
    select_related_extra_conditions: Mapping[str, Any]
    distinct_on: tuple[str, ...]
    group_bys: tuple[str, ...]
    alias_keys: frozenset[str]
    does_not_exist_exception: type[BaseException] | BaseException | None
    is_single_row_of_slice: bool
    default_ordering_disabled: bool
    fields_for_select: tuple[str, ...]
    deferred_fields: tuple[str, ...]
    deferred_related_fields: frozenset[str]
    explicitly_select_related: frozenset[str]
    calls_before_setup: CallsBeforeSetup | None

    #: The options of a query that set none of them.
    #: The settings of how a queryset returns its rows - a query made from the queryset to count,
    #: summarize or write its rows takes none of them.
    ROW_RETURN_SETTINGS: ClassVar[tuple[str, ...]] = (
        "select_for_update",
        "select_for_update_nowait",
        "select_for_update_skip_locked",
        "select_for_update_of",
        "select_for_update_no_key",
        "select_related_extra_conditions",
        "does_not_exist_exception",
        "is_single_row_of_slice",
        "fields_for_select",
        "deferred_fields",
        "deferred_related_fields",
        "explicitly_select_related",
    )

    DEFAULT: ClassVar[QueryOptions]
    #: Copies every setting of one object onto another - see get_copy_function().
    copy_function: ClassVar[Callable[[QueryOptions, QueryOptions], None] | None] = None

    __slots__ = tuple(DEFAULTS)

    @staticmethod
    def freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
        """A read-only copy of ``value``.

        Args:
            value: The mapping.

        Returns:
            ``value`` itself when already read-only, else a read-only copy.
        """
        return value if isinstance(value, MappingProxyType) else MappingProxyType(dict(value))

    #: How an assigned collection is made immutable, by setting - a list becomes a tuple, a set a
    #: frozenset, a dict a read-only mapping. A setting not named here is stored as given.
    FREEZERS: ClassVar[dict[str, Callable[[Any], Any]]] = {
        "select_for_update_of": frozenset,
        "cursor_values": tuple,
        "before_cursor_values": tuple,
        "with_ctes": tuple,
        "extension_calls": tuple,
        "select_related_extra_conditions": freeze_mapping,
        "distinct_on": tuple,
        "group_bys": tuple,
        "alias_keys": frozenset,
        "fields_for_select": tuple,
        "deferred_fields": tuple,
        "deferred_related_fields": frozenset,
        "explicitly_select_related": frozenset,
    }

    @classmethod
    def build_default(cls) -> QueryOptions:
        """An object holding every setting's unset value.

        Returns:
            The object.
        """
        options = cls.__new__(cls)
        for name, value in cls.DEFAULTS.items():
            setattr(options, name, value)
        return options

    @classmethod
    def get_copy_function(cls) -> Callable[[QueryOptions, QueryOptions], None]:
        """``def copy(source, target): target.a = source.a; ...`` for every setting - one
        attribute assignment per slot, generated once, instead of a getattr()/setattr() loop.

        Returns:
            The function.
        """
        copy_function = cls.copy_function
        if copy_function is None:
            body = "\n".join(f"    target.{name} = source.{name}" for name in cls.__slots__)
            namespace: dict[str, Any] = {}
            exec(f"def copy(source, target):\n{body}", namespace)  # noqa: S102 # nosec B102 - class-derived source, no external input
            copy_function = cls.copy_function = namespace["copy"]
        return copy_function

    def without(self, *names: str) -> QueryOptions:
        """The options with the named settings unset.

        Args:
            names: The setting names.

        Returns:
            A new object, or this one itself when none of them is set.
        """
        if self is QueryOptions.DEFAULT:
            return self
        defaults = QueryOptions.DEFAULTS
        return self.updated(**{name: defaults[name] for name in names})

    def updated(self, **values: Any) -> QueryOptions:
        """The options with ``values`` assigned: each value made immutable (``FREEZERS``), and
        only a value that changes something - not the one already held, nor an unset value over
        an unset one - taken. Assigning several settings this way makes at most one new object.

        Args:
            values: Setting name -> value.

        Returns:
            A new object, or this one itself when nothing changes.
        """
        if self is QueryOptions.DEFAULT:
            # The common case - a query built from one that set nothing - checked without a call.
            for value in values.values():
                if value:
                    break
            else:
                return self
        options = self
        for name, value in values.items():
            freeze = QueryOptions.FREEZERS.get(name)
            if freeze is not None and value is not None:
                value = freeze(value)
            current = getattr(options, name)
            if value is current or (not value and not current):
                continue
            if options is self:
                options = QueryOptions.__new__(QueryOptions)
                QueryOptions.get_copy_function()(self, options)
            setattr(options, name, value)
        return options

    def is_default(self) -> bool:
        """Whether every setting holds its unset value.

        Returns:
            True when none is set.
        """
        return self is QueryOptions.DEFAULT or not any(getattr(self, name) for name in QueryOptions.__slots__)


QueryOptions.DEFAULT = QueryOptions.build_default()
