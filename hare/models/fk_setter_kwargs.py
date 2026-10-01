from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from hare.ddl.constraints.unique_constraint import UniqueConstraint

if TYPE_CHECKING:
    from hare.ddl.constraints.check_constraint import CheckConstraint
    from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
    from hare.ddl.constraints.foreign_key_constraint import ForeignKeyConstraint
    from hare.ddl.constraints.unique_constraint import UniqueConstraint

    ModelConstraint = UniqueConstraint | CheckConstraint | ExclusionConstraint | ForeignKeyConstraint


class FkSetterKwargs(TypedDict):
    _key: str
    relation_fields: tuple[str, ...]
    to_fields: tuple[str, ...]
