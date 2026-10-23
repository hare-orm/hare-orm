from __future__ import annotations

import reprlib
from typing import Any, ClassVar

from hare.classes.constants import (
    SHOWN_VALUE_MAX_ITEMS,
    SHOWN_VALUE_MAX_LEVELS,
    SHOWN_VALUE_MAX_TEXT_LENGTH,
)
from hare.classes.shown_value_repr import ShownValueRepr


class ValueDescriptions:
    """How an error message names a value it refuses - for its shape by its type and length, never
    its content, which may be a sensitive field's; or by a repr cut short."""

    #: The repr of a value as an error message shows it - long texts, big collections and deep
    #: nesting cut short, a container holding itself written once.
    SHOWN_VALUE_REPR: ClassVar[reprlib.Repr] = ShownValueRepr(
        maxlevel=SHOWN_VALUE_MAX_LEVELS,
        maxtuple=SHOWN_VALUE_MAX_ITEMS,
        maxlist=SHOWN_VALUE_MAX_ITEMS,
        maxarray=SHOWN_VALUE_MAX_ITEMS,
        maxdict=SHOWN_VALUE_MAX_ITEMS,
        maxset=SHOWN_VALUE_MAX_ITEMS,
        maxfrozenset=SHOWN_VALUE_MAX_ITEMS,
        maxdeque=SHOWN_VALUE_MAX_ITEMS,
        maxstring=SHOWN_VALUE_MAX_TEXT_LENGTH,
        maxlong=SHOWN_VALUE_MAX_TEXT_LENGTH,
        maxother=SHOWN_VALUE_MAX_TEXT_LENGTH,
    )

    @staticmethod
    def get_shown_value(value: Any) -> str:
        """The repr of a value an error message shows - cut short past
        ``SHOWN_VALUE_MAX_TEXT_LENGTH`` characters, ``SHOWN_VALUE_MAX_ITEMS`` items and
        ``SHOWN_VALUE_MAX_LEVELS`` levels; a message never grows with the value, nor fails on a deep one.

        Args:
            value: The value.

        Returns:
            The repr.
        """
        return ValueDescriptions.SHOWN_VALUE_REPR.repr(value)

    @staticmethod
    def get_shown_text(value: Any) -> str:
        """The ``str()`` of a value an error message quotes itself, cut short past
        ``SHOWN_VALUE_MAX_TEXT_LENGTH`` characters - a collection as ``get_shown_value()`` shows it.

        Args:
            value: The value.

        Returns:
            The text.
        """
        # An int's text past the interpreter's digit limit raises - its repr is cut short instead.
        if isinstance(value, (list, tuple, dict, set, frozenset)) or type(value) is int:
            return ValueDescriptions.get_shown_value(value)
        text = str(value)
        if len(text) <= SHOWN_VALUE_MAX_TEXT_LENGTH:
            return text
        return f"{text[:SHOWN_VALUE_MAX_TEXT_LENGTH]}..."

    @staticmethod
    def get_description(value: Any) -> str:
        """The value's type, and the number of its items for a collection.

        Args:
            value: The value refused.

        Returns:
            ``"None"``, ``"str"``, ``"tuple of 1 value"``, ``"list of 3 values"``.
        """
        if value is None:
            return "None"
        if isinstance(value, (list, tuple, set, frozenset, dict)):
            return f"{type(value).__name__} of {len(value)} value{'' if len(value) == 1 else 's'}"
        return type(value).__name__
