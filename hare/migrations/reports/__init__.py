"""What a migration is going to do, known before it runs: the operations a change of models needs,
and what each operation does to the data."""

from __future__ import annotations

from hare.migrations.reports.operation_effect import OperationEffect
from hare.migrations.reports.operation_plan import OperationPlan

__all__ = [
    "OperationEffect",
    "OperationPlan",
]
