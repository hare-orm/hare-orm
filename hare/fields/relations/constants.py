from __future__ import annotations

#: The DDL behind on_delete=PROTECT, which Python enforces before the DELETE: a deferrable NO ACTION
#: - a hard delete defers it for its own DELETE, so a guard the same cascade removes doesn't fail by
#: deletion order. Immediate by default, leaving no pending trigger events.
PROTECT_DB_ON_DELETE_SQL = "NO ACTION DEFERRABLE INITIALLY IMMEDIATE"

#: The end of the name of a GenericForeignKeyField's CHECK - ``target_exclusive_arc``.
GENERIC_FOREIGN_KEY_CHECK_SUFFIX = "_exclusive_arc"
