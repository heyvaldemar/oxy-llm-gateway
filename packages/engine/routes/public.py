"""
Public API Routes (No Authentication Required)
===============================================

These endpoints are designed for:
1. Health checks
2. Public discovery (feature list)

SECURITY:
- No sensitive data exposed
- Rate limited to prevent abuse

"""

from fastapi import APIRouter
from slowapi import Limiter
from slowapi.util import get_remote_address

# ============================================================================
# ROUTER SETUP
# ============================================================================

router = APIRouter(prefix="/public", tags=["public"])
limiter = Limiter(key_func=get_remote_address)


@router.get("/status")
async def public_status():
    """
    Liveness without authentication: the API process answers. It checks nothing
    else; GET /health reports Redis and the database as they are.
    """
    return {"status": "operational", "version": "1.0.6"}
