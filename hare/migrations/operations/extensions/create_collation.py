from __future__ import annotations

from typing import TYPE_CHECKING

from hare.core.log import logger
from hare.exceptions import ConfigurationError
from hare.migrations.operations.constants import COLLATION_PROVIDERS
from hare.migrations.operations.hare_operation import HareOperation
from hare.migrations.state.state import State
from hare.sql.constants import COLLATION_NAME_PATTERN

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


class CreateCollation(HareOperation):
    """Creates a Postgres collation - e.g. a case-insensitive ICU one for ``Collate(field, name)``
    and ``CharField(db_collation=...)``. SQLite has no such DDL: it's skipped there with a warning.

    Args:
        name: The collation's name.
        locale: Its locale, e.g. ``"und-u-ks-level2"``.
        provider: ``"libc"`` or ``"icu"``.
        deterministic: False for a collation equal strings can differ under (ICU only).

    Raises:
        ConfigurationError: The name isn't a plain collation name, or the provider is unknown.
    """

    def __init__(self, name: str, locale: str, *, provider: str = "libc", deterministic: bool = True) -> None:
        if not COLLATION_NAME_PATTERN.fullmatch(name):
            raise ConfigurationError(f"Invalid collation name {name!r}")
        if provider not in COLLATION_PROVIDERS:
            raise ConfigurationError(
                f"Collation provider must be one of {sorted(COLLATION_PROVIDERS)}, got {provider!r}"
            )
        self.name = name
        self.locale = locale
        self.provider = provider
        self.deterministic = deterministic

    def describe(self) -> str:
        return f"Create collation {self.name}"

    async def _create(self, state_editor: BaseSchemaEditor | None) -> None:
        if not state_editor:
            return
        if not state_editor.client.features.supports_collations:
            logger.warning(
                "Skipping the collation %r on %s - it creates no collations.", self.name, state_editor.client.dialect
            )
            return
        await state_editor.extensions.create_collation(self.name, self.locale, self.provider, self.deterministic)

    async def _drop(self, state_editor: BaseSchemaEditor | None) -> None:
        if not state_editor:
            return
        if not state_editor.client.features.supports_collations:
            logger.warning(
                "Skipping DROP COLLATION %r on %s - it has no such DDL.", self.name, state_editor.client.dialect
            )
            return
        await state_editor.extensions.drop_collation(self.name)

    async def database_forward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        await self._create(state_editor)

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        await self._drop(state_editor)
