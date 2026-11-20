class SqliteEncodedText(str):
    """An internal text envelope whose elements have already been adapted.

    A packed membership parameter must not run a user text adapter on its JSON envelope
    after adapting the individual elements. It remains text for SQL literal rendering.
    """
