from dataclasses import dataclass


@dataclass(frozen=True, slots=True, eq=False)
class SourceBinding:
    """Identity of a source occurrence, independent of its printed SQL alias."""
