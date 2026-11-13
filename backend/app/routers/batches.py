from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import reconcile_service as svc

router = APIRouter(tags=["batches"])


class RunBody(BaseModel):
    pair_id: int
    note: str | None = None
    extract_delay_ms: int = 0  # demo knob: widen the in-flight window


@router.get("/batches")
def list_batches(limit: int = 50):
    return svc.list_batches(limit=limit)


@router.get("/diffs")
def list_diffs(batch_id: int | None = None, pair_id: int | None = None):
    return svc.list_diffs(batch_id=batch_id, pair_id=pair_id)


@router.post("/reconcile")
def run_reconcile(body: RunBody):
    try:
        return svc.run_reconcile(body.pair_id, body.note, extract_delay_ms=body.extract_delay_ms)
    except svc.ConcurrentRunError as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
