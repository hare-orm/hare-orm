"""Who reaches a model's rows - row level security policies and privileges granted to roles."""

from __future__ import annotations

from hare.ddl.security.grant import Grant
from hare.ddl.security.policy import Policy

__all__ = ["Grant", "Policy"]
