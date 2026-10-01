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


class TriggerEvent(StrEnum):
    """The event a trigger fires on. A combination ("INSERT OR UPDATE", "UPDATE OF col1, col2") is
    written as text in ``Trigger.on``.
    """

    INSERT = "INSERT"
    UPDATE = "UPDATE"
    DELETE = "DELETE"


class TriggerTiming(StrEnum):
    BEFORE = "BEFORE"
    AFTER = "AFTER"
    #: PostgreSQL has it (on views only) - SQLite has no INSTEAD OF trigger support.
    INSTEAD_OF = "INSTEAD OF"


class TriggerForEach(StrEnum):
    ROW = "ROW"
    #: Needs ``Dialect.supports_statement_triggers`` - SQLite triggers always fire per row.
    STATEMENT = "STATEMENT"
