from __future__ import annotations

#: Placeholder a password or other secret connection-config value is replaced with in logs and reprs.
PASSWORD_LOG_MASK = "***"  # nosec B105 - a masking placeholder, not a real credential

#: Lower-cased fragments marking a connection-config key (credential, DB_URL query parameter) as
#: secret - its value is masked in startup logs and in connection-config reprs.
SECRET_CONFIG_KEY_MARKERS = ("password", "passwd", "pwd", "secret", "token")
