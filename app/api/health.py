"""``GET /health``: no launch token needed, never personal data (ADR-023)."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.services.health import HealthService

router = APIRouter()


@router.get("/health")
def health(request: Request) -> JSONResponse:
    service: HealthService = request.app.state.health
    report = service.check()
    body = {"version": report.version, "database": report.database, "runner": report.runner}
    return JSONResponse(body, status_code=200 if report.ok else 503)
