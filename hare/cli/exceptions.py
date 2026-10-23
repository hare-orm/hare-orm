from __future__ import annotations


class CLIError(Exception):
    pass


class CLIUsageError(CLIError):
    pass
