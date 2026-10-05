"""The ZooKeeper protocol ClickHouse Keeper speaks, as far as hare's row locks use it."""

#: The protocol version of the connect request.
KEEPER_PROTOCOL_VERSION = 0
#: The password of a new session - none.
KEEPER_NEW_SESSION_PASSWORD = b"\x00" * 16
#: The session timeout asked for, in milliseconds - the server may give another.
KEEPER_SESSION_TIMEOUT_MILLISECONDS = 30000
#: How many pings a session timeout takes.
KEEPER_PINGS_PER_SESSION_TIMEOUT = 3

#: The operations of a request.
KEEPER_CREATE_OPERATION = 1
KEEPER_DELETE_OPERATION = 2
KEEPER_EXISTS_OPERATION = 3
KEEPER_PING_OPERATION = 11
KEEPER_CLOSE_SESSION_OPERATION = -11

#: The request ids of the replies that answer no request of their own.
KEEPER_WATCH_EVENT_REQUEST_ID = -1
KEEPER_PING_REQUEST_ID = -2

#: The error codes of a reply.
KEEPER_NO_ERROR = 0
KEEPER_NO_NODE_ERROR = -101
KEEPER_NODE_EXISTS_ERROR = -110

#: The flags of a node - kept, or deleted as its session ends.
KEEPER_PERSISTENT_NODE = 0
KEEPER_EPHEMERAL_NODE = 1
#: The access of every node: anyone may do anything.
KEEPER_OPEN_ACL_PERMISSIONS = 31
KEEPER_OPEN_ACL_SCHEME = "world"
KEEPER_OPEN_ACL_ID = "anyone"
#: Any version of a node, for a delete.
KEEPER_ANY_VERSION = -1

#: The node the row locks of every database live under.
KEEPER_ROW_LOCKS_PATH = "/hare/locks"
#: How many row locks a transaction may hold.
KEEPER_ROW_LOCK_LIMIT = 100000
#: The port of a Keeper address given without one.
KEEPER_DEFAULT_PORT = 9181
