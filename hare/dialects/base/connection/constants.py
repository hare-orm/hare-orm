from __future__ import annotations

from hare.dialects.base.connection.connection_option import ConnectionOption
from hare.dialects.base.connection.connection_options import ConnectionOptions
from hare.dialects.enums import ConnectionOptionType

#: How long a password from ``password_provider`` is used before the provider is asked again, when
#: ``password_refresh_seconds`` isn't set - a cloud IAM token lives 15 minutes.
DEFAULT_PASSWORD_REFRESH_SECONDS = 600.0
#: The settings of a connection that takes its password from a function (``PasswordProvider``) - a
#: dialect whose clients renew their password adds them to its own settings.
PASSWORD_PROVIDER_OPTIONS = ConnectionOptions(
    ConnectionOption("password_provider", ConnectionOptionType.CALLABLE),
    ConnectionOption("password_refresh_seconds", ConnectionOptionType.SECONDS),
)

DB_URL_USERINFO_DELIMITERS = frozenset(":/?#[]@")
DB_URL_INVALID_ESCAPE_PATTERN = r"%(?![0-9a-fA-F]{2})"
