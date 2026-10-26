from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any

import pytest


@dataclasses.dataclass(frozen=True)
class MetaOptionSample:
    """Two values of a ``Meta`` option - what an abstract base declares and what a concrete model overrides it
    with.

    Attributes:
        option: The option's name.
        make_base_value: Makes the base's value, anew for each call.
        make_override_value: Makes the concrete model's value.
        companion_options: Options the base and the control declare with it, for the option to be valid.
        requires: The extras the option needs.
        base_model_path: The model an extension reads the option of, the abstract base is built on it.
        cleared_value: What a concrete model names to have none of the option - None, an empty collection for a
            collection option.
    """

    option: str
    make_base_value: Callable[[], Any]
    make_override_value: Callable[[], Any]
    companion_options: dict[str, Any] = dataclasses.field(default_factory=dict)
    requires: tuple[str, ...] = ()
    base_model_path: str | None = None
    cleared_value: Any = None

    def get_parameter(self, *marks: Any) -> Any:
        """The sample as a test's parameter, requiring its extras.

        Args:
            marks: Further marks.

        Returns:
            The parameter, named after the option.
        """
        requirement_marks = [pytest.mark.requires(*self.requires)] if self.requires else []
        return pytest.param(self, id=self.option, marks=[*requirement_marks, *marks])
