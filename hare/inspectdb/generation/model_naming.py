"""The database-name-to-identifier mangling SchemaIntrospector and ModelSourceBuilder share - the
introspector predicts the attribute names the generator gives.
"""

from __future__ import annotations

import keyword
import unicodedata

from hare.inspectdb.constants import ENUM_MEMBER_NAME_PATTERN, UNDERSCORE_RUN_PATTERN


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
    def get_field_name(name: str) -> str:
        """A field's name for a column: ``get_safe_identifier()``'s, a run of underscores made one -
        ``__`` separates the names of a lookup path -, none at its end - a lookup path would read it as
        part of the separator -, and a keyword followed by ``_field``. The caller keeps the real name
        (``source_field=``) and the results unique.

        Args:
            name: The column's name.

        Returns:
            The field's name.
        """
        safe = ModelNaming.get_safe_identifier(name, digit_prefix="field_")
        if keyword.iskeyword(safe.removesuffix("_")) and safe.endswith("_"):
            safe = f"{safe.removesuffix('_')}_field"
        return UNDERSCORE_RUN_PATTERN.sub("_", safe).rstrip("_") or "field"

    @staticmethod
    def get_class_name(table_name: str) -> str:
        """Derives the Python class name inspectdb generates for a given DB table name - the single
        source of truth every caller that needs to name or reference a generated model class relies
        on, so they can never disagree about what name a given table maps to."""
        return ModelNaming.get_safe_identifier(
            "".join(part.capitalize() for part in table_name.split("_")), digit_prefix="T"
        )

    @staticmethod
    def get_enum_member_names(labels: list[str]) -> dict[str, str]:
        """The member names of an enum of labels - each label uppercased, a run of characters no name
        holds made an underscore, a number appended to a repeated name.

        Args:
            labels: The labels.

        Returns:
            The labels by member name.
        """
        member_names: dict[str, str] = {}
        for label in labels:
            member_name = ENUM_MEMBER_NAME_PATTERN.sub("_", label).strip("_").upper() or "VALUE"
            if not member_name[0].isalpha():
                member_name = f"VALUE_{member_name}"
            unique_member_name, number = member_name, 2
            while unique_member_name in member_names:
                unique_member_name, number = f"{member_name}_{number}", number + 1
            member_names[unique_member_name] = label
        return member_names
