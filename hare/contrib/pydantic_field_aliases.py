from __future__ import annotations

from pydantic import AliasChoices, AliasPath, ConfigDict
from pydantic.fields import FieldInfo


class PydanticFieldAliases:
    """Normalize Pydantic input aliases without interpreting field values."""

    @staticmethod
    def get_paths(name: str, field: FieldInfo, config: ConfigDict) -> tuple[tuple[str | int, ...], ...]:
        """Return accepted input paths in Pydantic's precedence order.

        Validation aliases override serialization aliases. Name fallback follows
        the model configuration; fields without an alias always accept their name.
        """
        alias = field.validation_alias
        paths: list[tuple[str | int, ...]] = []
        if config.get("validate_by_alias", True):
            if isinstance(alias, str):
                paths.append((alias,))
            elif isinstance(alias, AliasChoices):
                paths.extend(tuple(path) for path in alias.convert_to_aliases())
            elif isinstance(alias, AliasPath):
                paths.append(tuple(alias.convert_to_aliases()))
        if alias is None or config.get("validate_by_name", config.get("populate_by_name", False)):
            paths.append((name,))
        return tuple(dict.fromkeys(paths))
