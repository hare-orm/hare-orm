CONNECTION_HANDLER_CLOSING = "The application's connections are closing; new connections cannot be acquired"
CONNECTION_RELEASE_FAILED = "Connection resources could not all be closed"
FOREIGN_CONTEXT_CLIENT_MESSAGE = (
    "The database client belongs to another HareContext - build the query from that context "
    "or configure the database in this context"
)

#: The refusal of a query on a model without a default connection, by the model's name.
MISSING_DEFAULT_CONNECTION_MESSAGE = (
    "{model_name} has no default connection - set the apps.<app>.default_connection key of Hare.init()"
)
