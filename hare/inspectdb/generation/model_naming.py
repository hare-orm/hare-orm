"""The database-name-to-identifier mangling SchemaIntrospector and ModelSourceGenerator share - the
introspector predicts the attribute names the generator gives.
"""

from __future__ import annotations

import keyword
import unicodedata


class ModelNaming:
    """Python names of the models and fields inspectdb generates."""

    @staticmethod
    def get_safe_identifier(name: str, *, digit_prefix: str) -> str:
        """``name`` made a usable identifier: an illegal character becomes an underscore (letters of
        any script stay), a leading digit gets ``digit_prefix``, a keyword gets a trailing
        underscore. The caller keeps the real name (``source_field=``) and the results unique.
        """
        normalized = unicodedata.normalize("NFKC", name)
        safe = "".join(char if f"a{char}".isidentifier() else "_" for char in normalized)
        if not safe or not safe.isidentifier():
            safe = f"{digit_prefix}{safe}"
        return f"{safe}_" if keyword.iskeyword(safe) else safe

    @staticmethod
    def get_class_name(table_name: str) -> str:
        """Derives the Python class name inspectdb generates for a given DB table name - the single
        source of truth every caller that needs to name or reference a generated model class relies
        on, so they can never disagree about what name a given table maps to."""
        return ModelNaming.get_safe_identifier(
            "".join(part.capitalize() for part in table_name.split("_")), digit_prefix="T"
        )
