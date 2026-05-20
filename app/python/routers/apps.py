"""`/api/v1/apps/*` endpoints.

Phase 1 ports only `verify_credentials`. Registering an app (`POST
/api/v1/apps`) belongs with OAuth issuance and lands when that flow
ports — until then the legacy backend still serves it.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.python.deps import CurrentApplication
from app.python.schemas.application import Application

router = APIRouter(prefix="/api/v1/apps", tags=["apps"])


@router.get("/verify_credentials", response_model=Application)
async def verify_credentials(app: CurrentApplication) -> Application:
    return Application.from_model(app)
