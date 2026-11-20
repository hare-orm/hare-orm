from hare.exceptions import DoesNotExist


class InstanceUpdateGuardMismatch(DoesNotExist):
    """The row no longer matches an instance update's expected values."""
