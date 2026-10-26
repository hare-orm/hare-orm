from __future__ import annotations

from typing import TYPE_CHECKING

from hare.classes.lazy_exports import LazyExports
from hare.ddl.constants import EXPORTED_MODULES

if TYPE_CHECKING:
    from hare.ddl.conditions.enum_members_condition import EnumMembersCondition
    from hare.ddl.conditions.exclusive_arc_condition import ExclusiveArcCondition
    from hare.ddl.conditions.tenant_condition import TenantCondition
    from hare.ddl.constraints.check_constraint import CheckConstraint
    from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
    from hare.ddl.constraints.unique_constraint import UniqueConstraint
    from hare.ddl.enums import (
        ExclusionConstraintUsing,
        FunctionVolatility,
        GrantTarget,
        PolicyCommand,
        Privilege,
        RowLevelSecurity,
        TriggerEvent,
        TriggerForEach,
        TriggerTiming,
    )
    from hare.ddl.indexes.index import Index
    from hare.ddl.indexes.partial_index import PartialIndex
    from hare.ddl.raw_sql_term import RawSQLTerm
    from hare.ddl.schema_objects.database_function import DatabaseFunction
    from hare.ddl.schema_objects.database_sequence import DatabaseSequence
    from hare.ddl.schema_objects.dictionary import Dictionary
    from hare.ddl.schema_objects.materialized_view import MaterializedView
    from hare.ddl.schema_objects.trigger import Trigger
    from hare.ddl.schema_objects.view import View
    from hare.ddl.security.grant import Grant
    from hare.ddl.security.policy import Policy

__all__ = [
    "CheckConstraint",
    "DatabaseFunction",
    "DatabaseSequence",
    "Dictionary",
    "EnumMembersCondition",
    "ExclusiveArcCondition",
    "ExclusionConstraint",
    "ExclusionConstraintUsing",
    "FunctionVolatility",
    "Grant",
    "GrantTarget",
    "Index",
    "MaterializedView",
    "PartialIndex",
    "Policy",
    "TenantCondition",
    "Trigger",
    "TriggerEvent",
    "TriggerForEach",
    "TriggerTiming",
    "PolicyCommand",
    "Privilege",
    "RawSQLTerm",
    "RowLevelSecurity",
    "UniqueConstraint",
    "View",
]


__getattr__, __dir__ = LazyExports(__name__, EXPORTED_MODULES).get_module_hooks()
