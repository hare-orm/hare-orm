class AllTenants:
    """Every tenant's rows - ``Tenancy.ALL``, as a whole tenant scope or as one model's entry of a
    scope given model by model."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "Tenancy.ALL"
