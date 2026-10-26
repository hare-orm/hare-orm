from __future__ import annotations

from typing import Any, ClassVar

import pytest

from hare import fields
from hare.exceptions import ValidationError
from hare.fields.validators import MinValueValidator
from tests.contracts.writes.enums import WriteOutcome, WriteRole
from tests.contracts.writes.write_observer import WriteObserver
from tests.contracts.writes.write_path import WritePath
from tests.contracts.writes.write_paths import WRITE_PATHS
from tests.contracts.writes.write_scenario import WriteScenario


class ValidatorsObserver(WriteObserver):
    """The validators of a field: a path writing a value the field's validators refuse is refused whole and
    changes no row; a path writing no such value is none the validators are about."""

    name: ClassVar[str] = "field_validators"
    outcomes: ClassVar[dict[str, WriteOutcome]] = {
        path.name: WriteOutcome.REFUSED if path.writes_amount else WriteOutcome.NOT_APPLICABLE for path in WRITE_PATHS
    }

    def get_field_attributes(self, role: WriteRole) -> dict[str, Any]:
        return {"amount": fields.IntField(default=0, validators=[MinValueValidator(0)])}

    async def check(self, scenario: WriteScenario, path: WritePath, outcome: WriteOutcome) -> None:
        before = await scenario.get_snapshot()
        if outcome is WriteOutcome.REFUSED:
            with pytest.raises(ValidationError):
                await path.run(scenario, -1)
            assert await scenario.get_snapshot() == before
        else:
            await path.run(scenario, -1)
