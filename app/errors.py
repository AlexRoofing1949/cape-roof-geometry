"""Typed, non-sensitive failures returned by the geometry endpoint."""


class GeometryServiceError(RuntimeError):
    """Base error with an HTTP-safe status code and retry classification."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int,
        retryable: bool = False,
        details: dict | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.retryable = retryable
        self.details = details or {}


class NoCoverageError(GeometryServiceError):
    def __init__(self, code: str, message: str):
        super().__init__(code, message, http_status=404, retryable=False)


class UnreliableGeometryError(GeometryServiceError):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(code, message, http_status=422, retryable=False, details=details)


class TransientProviderError(GeometryServiceError):
    def __init__(self, code: str, message: str):
        super().__init__(code, message, http_status=503, retryable=True)


class ConfigurationError(GeometryServiceError):
    def __init__(self, code: str, message: str):
        super().__init__(code, message, http_status=503, retryable=False)
