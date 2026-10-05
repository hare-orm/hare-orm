from __future__ import annotations

import re

from hare.ddl.enums import GrantTarget, PolicyCommand, Privilege

#: The smallest and the largest value of a sequence - a 64-bit integer.
SEQUENCE_VALUE_MIN = -(2**63)
SEQUENCE_VALUE_MAX = 2**63 - 1
#: The most values a session takes from a sequence in advance - a sanity ceiling against typos.
SEQUENCE_CACHE_MAX = 1_000_000

#: The name of a function's language - a plain identifier.
FUNCTION_LANGUAGE_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: The privileges a grant on each type of object can give.
GRANT_TARGET_PRIVILEGES: dict[GrantTarget, frozenset[Privilege]] = {
    GrantTarget.TABLE: frozenset(
        {
            Privilege.SELECT,
            Privilege.INSERT,
            Privilege.UPDATE,
            Privilege.DELETE,
            Privilege.TRUNCATE,
            Privilege.REFERENCES,
            Privilege.TRIGGER,
            Privilege.ALL,
        }
    ),
    GrantTarget.VIEW: frozenset(
        {
            Privilege.SELECT,
            Privilege.INSERT,
            Privilege.UPDATE,
            Privilege.DELETE,
            Privilege.TRUNCATE,
            Privilege.REFERENCES,
            Privilege.TRIGGER,
            Privilege.ALL,
        }
    ),
    GrantTarget.MATERIALIZED_VIEW: frozenset({Privilege.SELECT, Privilege.ALL}),
    GrantTarget.SEQUENCE: frozenset({Privilege.USAGE, Privilege.SELECT, Privilege.UPDATE, Privilege.ALL}),
    GrantTarget.FUNCTION: frozenset({Privilege.EXECUTE, Privilege.ALL}),
}
#: The privileges a grant can give on some columns of a table only.
COLUMN_PRIVILEGES = frozenset({Privilege.SELECT, Privilege.INSERT, Privilege.UPDATE, Privilege.REFERENCES})

#: The commands a policy has no ``using`` condition for - a new row is only checked.
POLICY_COMMANDS_WITHOUT_USING = frozenset({PolicyCommand.INSERT})
#: The commands a policy has no ``with_check`` condition for - no row is written.
POLICY_COMMANDS_WITHOUT_WITH_CHECK = frozenset({PolicyCommand.SELECT, PolicyCommand.DELETE})

#: The alias a trigger's ``Q`` condition is rendered against - written as the trigger's row
#: (``NEW``/``OLD``) once rendered.
TRIGGER_CONDITION_TABLE_ALIAS = "hare_trigger_row"
#: The events of a trigger whose condition reads the row being deleted (``OLD``) - any other reads
#: the row written (``NEW``).
TRIGGER_OLD_ROW_EVENTS = frozenset({"DELETE"})
#: The rows a trigger's condition reads - the one written and the one deleted.
TRIGGER_NEW_ROW = "NEW"
TRIGGER_OLD_ROW = "OLD"
