from __future__ import annotations

from hare.dialects.base.connection.connection_option import ConnectionOption
from hare.dialects.base.connection.connection_options import ConnectionOptions
from hare.dialects.enums import ConnectionOptionType

#: How long a password from ``password_provider`` is used before the provider is asked again, when
#: ``password_refresh_seconds`` isn't set - a cloud IAM token lives 15 minutes.
DEFAULT_PASSWORD_REFRESH_SECONDS = 600.0
#: The longest ``password_refresh_seconds`` - a day.
MAX_PASSWORD_REFRESH_SECONDS = 86400.0

#: The settings of a connection that takes its password from a function (``PasswordProvider``) - a
#: dialect whose clients renew their password adds them to its own settings.
PASSWORD_PROVIDER_OPTIONS = ConnectionOptions(
    ConnectionOption("password_provider", ConnectionOptionType.CALLABLE),
    ConnectionOption(
        "password_refresh_seconds", ConnectionOptionType.SECONDS, positive=True, maximum=MAX_PASSWORD_REFRESH_SECONDS
    ),
)

#: Host (reg-name or bracketed IPv6) and optional port right after a DB_URL's userinfo "@",
#: followed by the end of the authority.
#: A bracketed IPv6 host may carry a percent-encoded zone id ("[fe80::1%25eth0]").
DB_URL_AUTHORITY_HOST_PATTERN = (
    r"(?:\[[0-9A-Fa-f:.]*(?:%25[A-Za-z0-9._~%-]+)?\]|[A-Za-z0-9._~%!$&'()*+,;=-]*)(?::[0-9]*)?(?=[/?#]|$)"
)
