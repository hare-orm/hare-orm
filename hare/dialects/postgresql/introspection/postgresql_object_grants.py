from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from hare.ddl.constants import GRANT_TARGET_PRIVILEGES
from hare.ddl.enums import GrantTarget, Privilege
from hare.ddl.security.grant import Grant
from hare.inspectdb.introspection.enums import SchemaPartType
from hare.inspectdb.introspection.schema_part import SchemaPart


class PostgresqlObjectGrants:
    """Retain each overload's privileges, including grants no declaration can express."""

    @staticmethod
    def get_parts(
        rows: list[dict[str, Any]],
        target: GrantTarget,
        *,
        function_signatures: Mapping[str, tuple[str, ...]] | None = None,
    ) -> list[SchemaPart]:
        """Build declarations only when the function inventory proves name-only identity.

        Privilege rows alone cannot establish this: other overloads may have no grants.
        Unknown inventory or a grant outside its signatures stays a diagnostic/comparison
        part: inspected functions may exclude extension or trigger functions of the same name.
        """
        grouped: dict[tuple[str, str | None, str, bool], set[str]] = defaultdict(set)
        for row in rows:
            arguments = row.get("arguments")
            grouped[row["object_name"], arguments, row["role"], bool(row["is_grantable"])].add(
                str(row["privilege_type"]).lower()
            )
        parts = []
        for (name, arguments, role, grantable), privileges in grouped.items():
            definition = {
                "on": target.value,
                "object_name": name,
                "arguments": arguments,
                "role": role,
                "with_grant_option": grantable,
                "privileges": sorted(privileges),
            }
            grant = None
            unambiguous = target is not GrantTarget.FUNCTION or (
                function_signatures is not None and function_signatures.get(name) == (arguments,)
            )
            if privileges <= GRANT_TARGET_PRIVILEGES[target] and unambiguous:
                grant = Grant(
                    privileges=tuple(Privilege(privilege) for privilege in sorted(privileges)),
                    roles=(role,),
                    on=target,
                    object_name=name,
                    with_grant_option=grantable,
                )
            parts.append(SchemaPart(SchemaPartType.GRANT, "", repr(definition), definition, grant))
        return parts
