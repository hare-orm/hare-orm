from __future__ import annotations

from typing import Any, ClassVar

import pytest

from hare import fields
from hare.exceptions import FieldError
from hare.fields.encrypted.field_encryption import FieldEncryption
from hare.instrumentation.enums import RowOperation
from tests.contracts.writes.enums import WriteOutcome, WriteRole
from tests.contracts.writes.write_observer import WriteObserver
from tests.contracts.writes.write_path import WritePath
from tests.contracts.writes.write_paths import WRITE_PATHS
from tests.contracts.writes.write_scenario import WriteScenario

ENCRYPTED_ROLES = (WriteRole.SUBJECT, WriteRole.NOTE)
#: The paths writing rows in the database without passing through Python - the blind index couldn't be computed.
REFUSED_PATHS = ("QuerySet.insert_from", "QuerySet.merge")


class EncryptionObserver(WriteObserver):
    """An encrypted field with a blind index: a path writing the field stores its ciphertext and the index of
    its plaintext, reads it back as the plaintext; a path writing none leaves the stored value and the index
    of every row as they were."""

    name: ClassVar[str] = "encryption"
    requires: ClassVar[tuple[str, ...]] = ("encryption",)
    outcomes: ClassVar[dict[str, WriteOutcome]] = {
        path.name: (
            WriteOutcome.REFUSED
            if path.name in REFUSED_PATHS
            else WriteOutcome.OBSERVED
            if path.writes_amount
            else WriteOutcome.NOT_APPLICABLE
        )
        for path in WRITE_PATHS
    }

    def get_field_attributes(self, role: WriteRole) -> dict[str, Any]:
        return {"amount": fields.EncryptedTextField(blind_index=True)} if role in ENCRYPTED_ROLES else {}

    def get_amount_value(self, amount: int) -> Any:
        return str(amount)

    @staticmethod
    async def get_stored_values(scenario: WriteScenario) -> dict[str, dict[Any, tuple[Any, Any]]]:
        """The ciphertext and the blind index every row stores - read around the fields.

        Args:
            scenario: The scenario.

        Returns:
            Each model's ``(amount, amount_blind_index)`` by key.
        """
        stored: dict[str, dict[Any, tuple[Any, Any]]] = {}
        for role in ENCRYPTED_ROLES:
            model = scenario.models.get_model(role)
            connection = scenario.context.get_connection()
            literals = connection.dialect.literals
            rows = await connection.execute_dicts(
                f"SELECT {literals.quote_identifier('id')} AS id, {literals.quote_identifier('amount')} AS amount, "
                f"{literals.quote_identifier('amount_blind_index')} AS amount_blind_index "
                f"FROM {literals.quote_identifier(model._meta.db_table)}"
            )
            stored[role.value] = {row["id"]: (row["amount"], row["amount_blind_index"]) for row in rows}
        return stored

    async def check(self, scenario: WriteScenario, path: WritePath, outcome: WriteOutcome) -> None:
        before = await self.get_stored_values(scenario)
        if outcome is WriteOutcome.REFUSED:
            with pytest.raises(FieldError):
                await path.run(scenario, 5)
            assert await self.get_stored_values(scenario) == before
            return
        await path.run(scenario, 5)
        after = await self.get_stored_values(scenario)

        written = set()
        for role, operation, pk in path.get_changed_rows(*ENCRYPTED_ROLES):
            if operation is RowOperation.DELETE:
                assert pk not in after[role.value]
                continue
            if outcome is WriteOutcome.OBSERVED:
                written.add((role.value, pk))
                ciphertext, blind_index = after[role.value][pk]
                assert ciphertext != "5"
                assert blind_index == FieldEncryption.get_blind_index("5")
                row = await scenario.models.get_model(role).objects.include_deleted().get(pk=pk)
                assert row.amount == "5"
        for role in ENCRYPTED_ROLES:
            for pk, stored in before[role.value].items():
                if (role.value, pk) not in written and pk in after[role.value]:
                    assert after[role.value][pk] == stored, (role, pk)
