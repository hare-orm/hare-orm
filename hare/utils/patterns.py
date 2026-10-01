import re
from typing import Any


class LazyPattern:
    """A regular expression compiled on first use, not on import - for the patterns of a module every
    program imports but few use. The first read of an attribute (``match``, ``search``, ``sub``,
    ``pattern``, ...) compiles the pattern and keeps that attribute of the compiled pattern on this
    object.
    """

    def __init__(self, source: str, flags: int = 0) -> None:
        """
        Args:
            source: The regular expression.
            flags: The ``re`` flags it compiles with.
        """
        self.source = source
        self.source_flags = flags
        self.compiled: re.Pattern[str] | None = None

    def __getattr__(self, name: str) -> Any:
        """Compiles the pattern on first use and hands out the compiled pattern's attribute,
        kept on this object for the next read.

        Args:
            name: The attribute of ``re.Pattern`` asked for.

        Returns:
            The attribute.
        """
        if name in ("source", "source_flags", "compiled"):
            # Not set yet - an object built without __init__ (copy/pickle protocols).
            raise AttributeError(name)
        compiled = self.compiled
        if compiled is None:
            compiled = self.compiled = re.compile(self.source, self.source_flags)
        value = getattr(compiled, name)
        setattr(self, name, value)
        return value

    def __repr__(self) -> str:
        return f"LazyPattern({self.source!r}, {self.source_flags!r})"
