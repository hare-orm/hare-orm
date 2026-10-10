from __future__ import annotations

from enum import StrEnum


class ExclusionConstraintUsing(StrEnum):
    """The index access method backing an ExclusionConstraint - part of the SQL grammar
    (``EXCLUDE USING <using> (...)``), not a free-form string."""

    #: The only access method the range/geometry overlap operators (``&&`` etc) work with -
    #: almost always what you want.
    GIST = "gist"
    #: A space-partitioned variant of GiST - same operator support as GIST, better for some data
    #: distributions (e.g. many small, similarly-sized ranges).
    SPGIST = "spgist"
    #: Equality-only exclusion - valid, but a plain UniqueConstraint already covers this case more
    #: simply; only useful when combined with a genuinely non-equality operator on another column.
    BTREE = "btree"


class GeneratedNamePrefix(StrEnum):
    """Prefix of a generated index or constraint name."""

    INDEX = "idx"
    UNIQUE_INDEX = "uidx"
    UNIQUE_CONSTRAINT = "uid"
    PRIMARY_KEY = "pk"
    #: The CHECK (column IS NOT NULL) ``AlterColumnNotNullSafe`` validates before SET NOT NULL.
    NOT_NULL_CHECK = "nn"


class TriggerEvent(StrEnum):
    """The event a trigger fires on. A combination ("INSERT OR UPDATE", "UPDATE OF col1, col2") is
    written as text in ``Trigger.on``.
    """

    INSERT = "INSERT"
    UPDATE = "UPDATE"
    DELETE = "DELETE"


class TriggerTiming(StrEnum):
    """When a trigger fires - before, after or instead of the statement."""

    BEFORE = "BEFORE"
    AFTER = "AFTER"
    #: PostgreSQL has it (on views only) - SQLite has no INSTEAD OF trigger support.
    INSTEAD_OF = "INSTEAD OF"


class TriggerForEach(StrEnum):
    """Whether a trigger fires once per row or once per statement."""

    ROW = "ROW"
    #: Needs ``Features.supports_statement_triggers`` - SQLite triggers always fire per row.
    STATEMENT = "STATEMENT"


class FunctionVolatility(StrEnum):
    """What a ``DatabaseFunction`` promises about its result, for the planner."""

    #: Its result may change within one statement, and it may change the database.
    VOLATILE = "volatile"
    #: Its result stays the same within one statement for the same arguments.
    STABLE = "stable"
    #: Its result depends on its arguments only.
    IMMUTABLE = "immutable"


class PolicyCommand(StrEnum):
    """The statements a row level security ``Policy`` applies to."""

    ALL = "all"
    SELECT = "select"
    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"


class Privilege(StrEnum):
    """A privilege a ``Grant`` gives a role."""

    SELECT = "select"
    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"
    TRUNCATE = "truncate"
    REFERENCES = "references"
    TRIGGER = "trigger"
    #: Using a sequence - ``currval``/``nextval``.
    USAGE = "usage"
    #: Calling a function.
    EXECUTE = "execute"
    #: Every privilege the object has.
    ALL = "all"


class GrantTarget(StrEnum):
    """The type of object of a model a ``Grant`` is on."""

    TABLE = "table"
    VIEW = "view"
    MATERIALIZED_VIEW = "materialized_view"
    SEQUENCE = "sequence"
    FUNCTION = "function"


class RowLevelSecurity(StrEnum):
    """Whether row level security filters a table's rows - ``Meta.row_level_security``."""

    #: The table's policies apply to every role but the table's owner.
    ENABLED = "enabled"
    #: The table's policies apply to its owner too.
    FORCED = "forced"
