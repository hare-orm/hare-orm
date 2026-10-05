from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.queryset.pending_calls.calls_before_setup import CallsBeforeSetup


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
        "select_for_update_strength": None,
        "cursor_values": (),
        "before_cursor_values": (),
        "reverse_result_order": False,
        "with_ctes": (),
        "extension_calls": (),
        "select_related_extra_conditions": MappingProxyType({}),
        "distinct_on": (),
        "group_bys": (),
        "grouping_set": None,
        "table_sample": None,
        "alias_keys": frozenset(),
        "does_not_exist_exception": None,
        "multiple_objects_returned_exception": None,
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
    select_for_update_strength: Any
    cursor_values: tuple[Any, ...]
    before_cursor_values: tuple[Any, ...]
    reverse_result_order: bool
    with_ctes: tuple[tuple[str, Any], ...]
    extension_calls: tuple[Any, ...]
    select_related_extra_conditions: Mapping[str, Any]
    distinct_on: tuple[str, ...]
    group_bys: tuple[str, ...]
    grouping_set: Any
    table_sample: Any
    alias_keys: frozenset[str]
    does_not_exist_exception: type[BaseException] | BaseException | None
    multiple_objects_returned_exception: type[BaseException] | BaseException | None
    is_single_row_of_slice: bool
    default_ordering_disabled: bool
    fields_for_select: tuple[str, ...]
    deferred_fields: tuple[str, ...]
    deferred_related_fields: frozenset[str]
    explicitly_select_related: frozenset[str]
    calls_before_setup: CallsBeforeSetup | None

    #: The options of a query that set none of them.
    #: The settings of how a queryset returns its rows - a query made from the queryset to count,
    #: summarize or write its rows takes none of them. A ``Select(relation, extra_condition=...)``
    #: condition isn't one: a filter crossing the relation reads its rows through it.
    ROW_RETURN_SETTINGS: ClassVar[tuple[str, ...]] = (
        "select_for_update",
        "select_for_update_nowait",
        "select_for_update_skip_locked",
        "select_for_update_of",
        "select_for_update_strength",
        "does_not_exist_exception",
        "multiple_objects_returned_exception",
        "is_single_row_of_slice",
        "fields_for_select",
        "deferred_fields",
        "deferred_related_fields",
        "explicitly_select_related",
    )

    #: How each setting meets a plan key. A setting written into the SQL text is keyed by its value;
    #: a setting whose values a plan binds is described by the query (its structure keyed, its values
    #: bound); a setting of how the result is returned has no part in the SQL. Every setting is in
    #: exactly one of them - checked when the module loads.
    SQL_SETTINGS: ClassVar[tuple[str, ...]] = (
        "select_for_update",
        "select_for_update_nowait",
        "select_for_update_skip_locked",
        "select_for_update_of",
        "select_for_update_strength",
        "reverse_result_order",
        "distinct_on",
        "group_bys",
        "grouping_set",
        "table_sample",
        "alias_keys",
        "is_single_row_of_slice",
        "default_ordering_disabled",
        "fields_for_select",
        "deferred_fields",
        "deferred_related_fields",
        "explicitly_select_related",
    )
    DESCRIBED_SETTINGS: ClassVar[tuple[str, ...]] = (
        "cursor_values",
        "before_cursor_values",
        "with_ctes",
        "extension_calls",
        "select_related_extra_conditions",
    )
    RESULT_SETTINGS: ClassVar[tuple[str, ...]] = (
        "does_not_exist_exception",
        "multiple_objects_returned_exception",
        "calls_before_setup",
    )
    #: The settings a query keeps no plan with - their values are written into the SQL text.
    KEEPS_NO_PLAN_SETTINGS: ClassVar[tuple[str, ...]] = ("table_sample",)
    #: How a set setting of ``SQL_SETTINGS`` is put into a plan key when its value isn't one as it
    #: is - an unordered set in a fixed order, a grouping set as its own key.
    PLAN_KEY_VALUES: ClassVar[dict[str, Callable[[Any], Any]]] = {
        "select_for_update_of": lambda value: tuple(sorted(value)),
        "grouping_set": lambda value: value.get_plan_key(),
    }

    DEFAULT: ClassVar[QueryOptions]
    #: The part of a plan key the settings of ``DEFAULT`` make.
    DEFAULT_PLAN_KEY_PART: ClassVar[tuple[Any, ...]]
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
            exec(f"def copy(source, target):\n{body}", namespace)  # nosec B102 - class-derived source, no external input
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

    def get_plan_key_part(self) -> tuple[Any, ...]:
        """The part of a plan key these settings make - the value of each setting written into the
        SQL text.

        Returns:
            The part.
        """
        if self is QueryOptions.DEFAULT:
            return QueryOptions.DEFAULT_PLAN_KEY_PART
        return self.build_plan_key_part()

    def build_plan_key_part(self) -> tuple[Any, ...]:
        """Makes ``get_plan_key_part()`` - one value per setting of ``SQL_SETTINGS``, in their order.

        Returns:
            The part.
        """
        plan_key_values = QueryOptions.PLAN_KEY_VALUES
        part = []
        for name in QueryOptions.SQL_SETTINGS:
            value = getattr(self, name)
            make_plan_key_value = plan_key_values.get(name)
            part.append(make_plan_key_value(value) if make_plan_key_value is not None and value else value)
        return tuple(part)

    def keeps_no_plan(self) -> bool:
        """Whether a query with these settings keeps no plan - a setting is written into the SQL text
        as it is (``KEEPS_NO_PLAN_SETTINGS``).

        Returns:
            True when the query keeps none.
        """
        return self is not QueryOptions.DEFAULT and any(
            getattr(self, name) for name in QueryOptions.KEEPS_NO_PLAN_SETTINGS
        )

    @classmethod
    def raise_if_settings_unclassified(cls) -> None:
        """Rejects a setting that isn't in exactly one of ``SQL_SETTINGS``, ``DESCRIBED_SETTINGS`` and
        ``RESULT_SETTINGS``, and a setting keeping no plan that isn't among the ``SQL_SETTINGS``.

        Raises:
            TypeError: A setting is unclassified or classified twice.
        """
        classified = [*cls.SQL_SETTINGS, *cls.DESCRIBED_SETTINGS, *cls.RESULT_SETTINGS]
        if sorted(classified) != sorted(cls.DEFAULTS):
            unclassified = sorted(set(cls.DEFAULTS) - set(classified))
            repeated = sorted({name for name in classified if classified.count(name) > 1})
            raise TypeError(
                f"Every QueryOptions setting is in exactly one of SQL_SETTINGS, DESCRIBED_SETTINGS and "
                f"RESULT_SETTINGS - unclassified: {unclassified}, classified twice: {repeated}"
            )
        if not set(cls.KEEPS_NO_PLAN_SETTINGS) <= set(cls.SQL_SETTINGS):
            raise TypeError("QueryOptions.KEEPS_NO_PLAN_SETTINGS are among its SQL_SETTINGS")


QueryOptions.DEFAULT = QueryOptions.build_default()
QueryOptions.DEFAULT_PLAN_KEY_PART = QueryOptions.DEFAULT.build_plan_key_part()
QueryOptions.raise_if_settings_unclassified()
