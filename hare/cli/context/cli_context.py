class CLIContext:
    """The `hare` CLI's global options, passed to every command's ``run()``: where to load the
    Hare config from. Hand it to ``CommandContext.load_config()`` to get that config.

    Attributes:
        config: The ``-c``/``--config`` value - ``module.VARIABLE`` (``settings.HARE_ORM``) or the
            path of a .json/.yml config file - or None, when the config is located through the
            environment or ``pyproject.toml``.
    """

    def __init__(self, config: str | None) -> None:
        self.config = config
