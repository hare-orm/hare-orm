"""Keeper session settings and the fixed paths used by hare."""

#: The requested session timeout in seconds; Keeper can negotiate another value.
KEEPER_SESSION_TIMEOUT = 30
#: The node the migration lock of every database lives under, and its child lock name.
KEEPER_MIGRATION_LOCKS_PATH = "/hare/migrations"
KEEPER_MIGRATION_LOCK_NAME = "migrate"
#: The port of a Keeper address given without one.
KEEPER_DEFAULT_PORT = 9181
