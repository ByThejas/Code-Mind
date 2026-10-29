class CodemindError(Exception):
    """An error with a stable, client-safe code and message.

    `retryable` marks transient failures (network, upstream 5xx, rate limits) that a job may retry.
    """

    status_code: int = 400

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.headers = headers or {}
        if status_code is not None:
            self.status_code = status_code


class NotFoundError(CodemindError):
    status_code = 404
