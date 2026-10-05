from __future__ import annotations

import re

#: Most related keys one check of a written row's tenant-scoped relation targets looks up in a
#: single query - room under every backend's bind-parameter and expression-depth limits.
TENANT_RELATION_CHECK_BATCH_SIZE = 500

#: The placeholder of a connection's ``tenant_schema_template`` the tenant value takes.
TENANT_SCHEMA_PLACEHOLDER = "{tenant}"

#: A tenant schema template with its placeholder taken out - lower-case letters, digits and ``_``.
TENANT_SCHEMA_TEMPLATE_TEXT_PATTERN = re.compile(r"[a-z0-9_]*")

#: A tenant value a schema is named after - lower-case letters, digits and ``_``.
TENANT_SCHEMA_VALUE_PATTERN = re.compile(r"[a-z0-9_]+")
