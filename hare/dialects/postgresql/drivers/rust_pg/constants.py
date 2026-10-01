from hare.dialects.base.connection_option import ConnectionOption
from hare.dialects.base.connection_options import ConnectionOptions
from hare.dialects.enums import ConnectionOptionType

#: tokio-postgres's message for a statement it failed to encode for the server - raised before
#: anything is sent, so the connection stays usable.
RUST_PG_MESSAGE_ENCODING_ERROR_MESSAGE = "error encoding message to server"
#: Most bind parameters the Postgres wire protocol carries in one statement (an unsigned 16-bit
#: count) - an encoding failure for a longer parameter list is that limit.
RUST_PG_PROTOCOL_MAX_BIND_PARAMS = 65535

#: How long to wait for the connection pool to close gracefully before terminating it.
POOL_CLOSE_TIMEOUT_SECONDS = 10

#: The connection kwargs `pg.connect()` takes besides those every Postgres client reads - anything
#: else is rejected with ConfigurationError.
RUST_PG_EXTRA_KEYS = ("statement_cache_size", "ssl_mode", "ssl_root_cert")

#: The settings the rust driver takes besides POSTGRES_CONNECTION_OPTIONS.
RUST_PG_CONNECTION_OPTIONS = ConnectionOptions(
    ConnectionOption("ssl_mode", ConnectionOptionType.TEXT),
    ConnectionOption("ssl_root_cert", ConnectionOptionType.TEXT),
)

#: libpq's environment variables for connection settings left unset in the configuration, and the
#: host used when neither names one.
RUST_PG_HOST_ENVIRONMENT_VARIABLE = "PGHOST"
RUST_PG_USER_ENVIRONMENT_VARIABLE = "PGUSER"
RUST_PG_PASSWORD_ENVIRONMENT_VARIABLE = "PGPASSWORD"  # nosec B105 - an environment variable name
RUST_PG_DATABASE_ENVIRONMENT_VARIABLE = "PGDATABASE"
RUST_PG_DEFAULT_HOST = "localhost"
#: How many rows ``stream()`` takes off the portal per call when its ``chunk_size`` is left unset -
#: iterator()'s own default chunk size.
RUST_PG_DEFAULT_STREAM_BATCH_SIZE = 1000
