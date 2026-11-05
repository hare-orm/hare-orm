from __future__ import annotations

from typing import ClassVar


class ExtensionModelOptions:
    """The ``Meta`` options an extension of hare reads of its models besides the model options of hare
    itself - a model declaring an option neither knows is refused."""

    #: The names of the options.
    # Schema defaults can be declared without importing the optional Pydantic package.
    names: ClassVar[set[str]] = {"pydantic"}

    @classmethod
    def allow(cls, *names: str) -> None:
        """Lets models declare the options.

        Args:
            names: The names of the options.
        """
        cls.names.update(names)
