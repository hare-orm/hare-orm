from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.models.enums import DeletionAction

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.fields.relations.fields.backward_foreign_key_relation import BackwardForeignKeyRelation
    from hare.fields.relations.fields.declarations import BackwardOneToOneRelation
    from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
    from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
    from hare.models import Model
    from hare.query.queryset import QuerySet
    from hare.sql import Table
    from hare.sql.terms.criteria.criterion import Criterion


@dataclass(frozen=True, slots=True)
class CascadeTransition:
    """The action for a reached row and whether the database performs its physical delete."""

    action: DeletionAction | None
    by_database: bool


@dataclass(frozen=True, slots=True)
class ForeignKeyChange:
    """A selected FK action with its target rows bound before any writes."""

    backward_field: BackwardForeignKeyRelation[Any] | BackwardOneToOneRelation[Any]
    field: ForeignKeyFieldInstance[Any]
    querysets: tuple[QuerySet[Any], ...]


@dataclass(frozen=True, slots=True)
class ThroughChange:
    """A through-table action with the connection, table and key predicates already bound."""

    model: type[Model]
    field: ManyToManyFieldInstance[Any]
    connection: DatabaseClient
    table: Table
    criteria: tuple[Criterion, ...]


@dataclass(frozen=True, slots=True)
class PreparedDeletionWave:
    """Rows and relationship changes prepared for one dependency wave."""

    rows: dict[type[Model], dict[Any, DeletionAction | None]]
    foreign_key_changes: tuple[ForeignKeyChange, ...]
    through_changes: tuple[ThroughChange, ...]
