"""Authenticated HTTP endpoint consumed by Cape Coral Roofing Apps Script."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from functools import lru_cache

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from .config import Settings
from .errors import ConfigurationError, GeometryServiceError
from .models import GeometryRequest
from .pipeline import reconstruct_roof, runtime_dependencies
from .source_registry import load_registries

LOGGER = logging.getLogger("cape_roof_geometry")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI(
    title="Cape Coral Roofing Open Geometry Service",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
_job_semaphore: asyncio.Semaphore | None = None


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings.from_environment()


def job_semaphore(configured: Settings) -> asyncio.Semaphore:
    global _job_semaphore
    if _job_semaphore is None:
        _job_semaphore = asyncio.Semaphore(configured.max_concurrent_jobs)
    return _job_semaphore


def authorize(authorization: str | None = Header(default=None)) -> Settings:
    try:
        configured = settings()
    except ConfigurationError as error:
        raise HTTPException(status_code=503, detail=error.code) from error
    prefix = "Bearer "
    supplied = authorization[len(prefix) :] if authorization and authorization.startswith(prefix) else ""
    if not supplied or not secrets.compare_digest(supplied, configured.bearer_token):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return configured


@app.get("/healthz")
def health() -> JSONResponse:
    dependencies = runtime_dependencies()
    try:
        configured = settings()
        registries = load_registries(configured.lidar_registry_path, configured.imagery_registry_path)
        configured_ok = True
        source_commit = configured.service_commit
        registry_version = registries.version_hash
    except ConfigurationError as error:
        configured_ok = False
        source_commit = ""
        registry_version = ""
        LOGGER.error("configuration_error code=%s", error.code)
    ready = configured_ok and all(dependencies.values())
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ready" if ready else "not_ready",
            "configured": configured_ok,
            "dependencies": dependencies,
            "serviceCommit": source_commit,
            "registryVersion": registry_version,
        },
    )


@app.post("/v1/roof-geometry")
async def roof_geometry(request: GeometryRequest, configured: Settings = Depends(authorize)) -> JSONResponse:
    LOGGER.info("geometry_request request_id=%s", request.requestId)
    try:
        async with job_semaphore(configured):
            result = await asyncio.to_thread(reconstruct_roof, request, configured)
    except GeometryServiceError as error:
        LOGGER.warning(
            "geometry_rejected request_id=%s code=%s retryable=%s details=%s",
            request.requestId,
            error.code,
            error.retryable,
            json.dumps(error.details, sort_keys=True, separators=(",", ":")),
        )
        return JSONResponse(
            status_code=error.http_status,
            content={
                "available": False,
                "verificationStatus": "INSPECTION_REQUIRED",
                "pricingAllowed": False,
                "status": error.code,
                "errorCode": error.code,
                "retryable": error.retryable,
                "message": error.message,
                "details": error.details,
            },
        )
    except Exception:
        LOGGER.exception("geometry_unexpected_error request_id=%s", request.requestId)
        return JSONResponse(
            status_code=500,
            content={
                "available": False,
                "verificationStatus": "INSPECTION_REQUIRED",
                "pricingAllowed": False,
                "status": "INTERNAL_GEOMETRY_ERROR",
                "errorCode": "INTERNAL_GEOMETRY_ERROR",
                "retryable": False,
            },
        )
    LOGGER.info(
        "geometry_completed request_id=%s verification=%s pricing_allowed=%s confidence=%s",
        request.requestId,
        result["verificationStatus"],
        result["pricingAllowed"],
        result["geometry"]["confidence"],
    )
    return JSONResponse(status_code=200, content=result)
