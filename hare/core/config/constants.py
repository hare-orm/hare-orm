from __future__ import annotations

import re

#: Placeholder a password or other secret connection-config value is replaced with in logs and reprs.
PASSWORD_LOG_MASK = "***"  # nosec B105 - a masking placeholder, not a real credential

#: Lower-cased fragments marking a connection-config key (credential, DB_URL query parameter) as
#: secret - its value is masked in startup logs and in connection-config reprs.
SECRET_CONFIG_KEY_MARKERS = ("password", "passwd", "pwd", "secret", "token")

#: The model label a ``swappable`` config setting points at - ``app_label.ModelName``.
SWAPPABLE_MODEL_LABEL_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*")
