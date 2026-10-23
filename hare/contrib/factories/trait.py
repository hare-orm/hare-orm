"""The declarations of a factory that only hold values - no logic."""

from __future__ import annotations

from typing import Any


class Trait:
    """Values a factory's object gets when the trait's parameter is given true - declared in the
    factory's ``Params``::

        class Params:
            admin = Trait(is_staff=True, role="admin")


        await UserFactory.create(admin=True)

    Args:
        values: The values, by field name - the ones given to ``create()`` itself win.
    """

    def __init__(self, **values: Any) -> None:
        self.values = values
