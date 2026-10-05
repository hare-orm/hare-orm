from __future__ import annotations

from typing import Any, ClassVar, Literal, overload

from hare.exceptions import ConfigurationError
from hare.fields.constants import SLUG_PATTERN, UNICODE_SLUG_PATTERN
from hare.fields.data.constants import SLUG_FIELD_MAX_LENGTH
from hare.fields.data.text.char_field import CharField, TStr
from hare.fields.validators.formats.slug_validator import SlugValidator


class SlugField(CharField[TStr]):
    """A slug - ASCII letters, digits, hyphens and underscores (``my-first-post``), checked by
    ``SlugValidator``. Indexed unless ``db_index=False`` is passed.

    Args:
        max_length: The most characters the slug may have - 50 by default.
        allow_unicode: Accept any Unicode letter or digit too (``привет-мир``).
    """

    indexed_by_default: ClassVar[bool] = True

    @overload
    def __init__(
        self: SlugField[str],
        max_length: int = SLUG_FIELD_MAX_LENGTH,
        *,
        allow_unicode: bool = False,
        null: Literal[False] = False,
        **kwargs: Any,
    ) -> None: ...

    @overload
    def __init__(
        self: SlugField[str | None],
        max_length: int = SLUG_FIELD_MAX_LENGTH,
        *,
        allow_unicode: bool = False,
        null: Literal[True],
        **kwargs: Any,
    ) -> None: ...

    def __init__(self, max_length: int = SLUG_FIELD_MAX_LENGTH, *, allow_unicode: bool = False, **kwargs: Any) -> None:
        if not isinstance(allow_unicode, bool):
            raise ConfigurationError(f"SlugField: allow_unicode must be a bool, got {allow_unicode!r}")
        self.allow_unicode = allow_unicode
        # CharField's overloads bind self to CharField[str] or CharField[str | None] - TStr is one of them.
        super().__init__(max_length, **kwargs)  # type: ignore[misc]
        #: The validator checking the slug - made by the write codec itself.
        self.format_validator = SlugValidator(allow_unicode=allow_unicode)
        self.validators.append(self.format_validator)

    @property
    def constraints(self) -> dict[str, Any]:
        return {**super().constraints, "pattern": f"^{UNICODE_SLUG_PATTERN if self.allow_unicode else SLUG_PATTERN}$"}
