"""Typed, non-sensitive failures returned by the geometry endpoint."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path


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
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(
            code,
            message,
            http_status=503,
            retryable=True,
            details=details,
        )


def command_failure_details(
    command: list[str], return_code: int, diagnostic_output: str | None
) -> dict:
    """Summarize command failures without exposing arguments or raw output."""

    diagnostic = (diagnostic_output or "").strip()
    normalized = diagnostic.lower()
    if re.search(r"\b429\b|too many requests", normalized):
        failure_class = "UPSTREAM_RATE_LIMIT"
    elif re.search(r"\b503\b|service unavailable", normalized):
        failure_class = "UPSTREAM_UNAVAILABLE"
    elif "timed out" in normalized or "timeout" in normalized:
        failure_class = "UPSTREAM_TIMEOUT"
    elif any(token in normalized for token in ("could not resolve", "name or service not known", "dns")):
        failure_class = "NETWORK_DNS_FAILURE"
    elif any(token in normalized for token in ("ssl", "tls", "certificate")):
        failure_class = "NETWORK_TLS_FAILURE"
    else:
        failure_class = "NONZERO_EXIT"
    details = {
        "command": Path(command[0]).name if command else "unknown",
        "returnCode": int(return_code),
        "failureClass": failure_class,
    }
    if diagnostic:
        details["diagnosticFingerprintSha256"] = hashlib.sha256(
            diagnostic.encode("utf-8", errors="replace")
        ).hexdigest()
    return details


class ConfigurationError(GeometryServiceError):
    def __init__(self, code: str, message: str):
        super().__init__(code, message, http_status=503, retryable=False)
