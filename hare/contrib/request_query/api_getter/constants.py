from __future__ import annotations

#: What separates the parts of a path of values (``"partner.user_id"``).
API_PATH_SEPARATOR = "."

#: The values a path ends at - a text or a number has nothing a path goes on to.
JSON_SCALAR_TYPES = (str, bytes, int, float, bool)

#: The longest a request to a source may take by default.
DEFAULT_API_TIMEOUT_SECONDS = 10.0

#: The most requests of a source at a time by default.
DEFAULT_API_CONCURRENCY = 10

#: The most values one ``ApiRequestMode.QUERY_MANY`` request asks for by default.
DEFAULT_API_BATCH_SIZE = 200

#: The longest pause before the first repeat of a failed request by default.
DEFAULT_API_RETRY_BASE_DELAY_SECONDS = 0.5
#: The longest pause before any repeat by default.
DEFAULT_API_RETRY_MAX_DELAY_SECONDS = 10.0

#: The answers a failed request is repeated on by default - too many requests, and the gateway
#: answers of a service that is down for a while.
API_RETRIED_STATUS_CODES = frozenset({429, 502, 503, 504})

#: The header of an answer telling how long to wait before asking again.
API_RETRY_AFTER_HEADER = "Retry-After"

#: The status of an answer having nothing for the value asked.
API_NOT_FOUND_STATUS_CODE = 404

#: The messages of the errors of a source and of a target.
API_DATA_NOT_FOUND_MESSAGE = "API {source}: no data for {value!r}"
API_BATCH_NOT_FOUND_MESSAGE = "API {source}: no data for the values asked"
API_REQUEST_FAILED_MESSAGE = "API {source}: the request for {value!r} failed"
API_BATCH_REQUEST_FAILED_MESSAGE = "API {source}: the request for the values asked failed"
API_NO_KEY_VALUES_MESSAGE = "API {source}: the objects hold no value of {key!r}"
API_TARGET_VALUE_MISSING_MESSAGE = "{target}: a required value wasn't received"
API_VALUE_CONVERSION_FAILED_MESSAGE = "{target}: the value can't be read - {error}"
