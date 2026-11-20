from __future__ import annotations

from enum import StrEnum


class NowValueType(StrEnum):
    """The type of value a ``Now()`` database default fills its column with."""

    DATETIME = "datetime"
    DATE = "date"
    TIME = "time"


class OnDelete(StrEnum):
    """What deleting a row does to the rows a relation points from at it."""

    CASCADE = "CASCADE"
    RESTRICT = "RESTRICT"
    SET_NULL = "SET NULL"
    SET_DEFAULT = "SET DEFAULT"
    NO_ACTION = "NO ACTION"
    # Checked in Python before the DELETE is issued (by the deletion collector) -
    # not a real constraint action, so it has no SQL keyword of its own. DDL generation falls back
    # to a deferrable NO ACTION for it (ForeignKeyFieldInstance.db_on_delete) as a backstop.
    PROTECT = "PROTECT"


class RelationLoadStrategy(StrEnum):
    """The relation loading strategy a field declares for every query on its model: ``JOINED`` like
    ``.select_related(field)``, ``SELECT`` like ``.prefetch_related(field)``.
    ``QuerySet.defer_related()`` opts out.
    """

    JOINED = "joined"
    SELECT = "select"


class RelationType(StrEnum):
    """Which relation a relational field is - ``Field.relation_type``; ``None`` there for a
    field holding a plain value."""

    #: ``ForeignKeyField`` - many rows of this model point at one related row.
    FOREIGN_KEY = "foreign_key"
    #: ``OneToOneField`` - one row of this model points at one related row.
    ONE_TO_ONE = "one_to_one"
    #: ``ManyToManyField`` - rows on both sides are linked through a through table.
    MANY_TO_MANY = "many_to_many"
    #: The reverse side of a ``ForeignKeyField`` (its ``related_name``) - the many rows
    #: pointing at this one.
    BACKWARD_FOREIGN_KEY = "backward_foreign_key"
    #: The reverse side of a ``OneToOneField`` (its ``related_name``) - the one row pointing
    #: at this one.
    BACKWARD_ONE_TO_ONE = "backward_one_to_one"


class HeldValueStep(StrEnum):
    """One step from a container to a value it holds."""

    #: Every element of an array.
    ELEMENT = "element"
    #: Every key of a map.
    KEY = "key"
    #: Every value of a map.
    VALUE = "value"
    #: The element of a tuple at a position.
    TUPLE_ELEMENT = "tuple_element"


class NarrowedValueSource(StrEnum):
    """What the values a narrowed column held before are - what decides the text a value turns
    into when the column becomes a ``CharField``."""

    TEXT = "text"
    BOOLEAN = "boolean"
    OTHER = "other"


class ValueComputation(StrEnum):
    """What a value computed from a field's values is - the database may compute it in a wider type than
    the column keeps (``Field.get_computed_field()``)."""

    SUM = "sum"
    AVERAGE = "average"
    #: A standard deviation, a variance, a quantile.
    STATISTIC = "statistic"
    #: Arithmetic of the values with another number.
    ARITHMETIC = "arithmetic"
