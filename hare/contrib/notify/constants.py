#: Base delay (seconds) of the exponential reconnect backoff: ``backoff * 2**attempt``.
DEFAULT_RECONNECT_BACKOFF_SECONDS = 0.5

#: Upper bound (seconds) a single reconnect delay is clamped to by default.
DEFAULT_MAX_BACKOFF_SECONDS = 30.0

#: Largest exponent the exponential backoff ever uses - keeps ``backoff * 2**attempt`` finite
#: when reconnecting forever.
MAX_BACKOFF_EXPONENT = 32

#: How often an idle, healthy LISTEN connection is checked for having died on its own.
HEALTH_CHECK_INTERVAL_SECONDS = 1.0
