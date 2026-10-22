"""Read-only browser access; original source ingestion is never a public write API."""

from fastapi import APIRouter, Request

from ..auth import require_auth
from .ledger import list_research_runs, read_research_run

router = APIRouter(prefix="/api/research", tags=["research"])


@router.get("/runs")
async def runs(request: Request):
    return {"runs": list_research_runs(require_auth(request))}


@router.get("/runs/{run_id}")
async def run_detail(run_id: str, request: Request):
    return read_research_run(require_auth(request), run_id)
