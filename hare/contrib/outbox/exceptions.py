class DeliveryError(Exception):
    """Wraps whatever exception an app-provided ``deliver`` callable raised, for logging/
    dead-letter reporting - the original exception is always chained via ``__cause__``."""
