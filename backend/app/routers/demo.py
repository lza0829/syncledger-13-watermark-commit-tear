from fastapi import APIRouter, HTTPException

from app.services import demo_service as svc

router = APIRouter(tags=["demo"])


@router.post("/demo/advance/{pair_code}")
def advance(pair_code: str, rows: int = 1, mismatch: bool = False):
    try:
        return svc.advance_fixtures(pair_code, rows=rows, mismatch=mismatch)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/demo/regress/{pair_code}")
def regress(pair_code: str, side: str = "both"):
    try:
        return svc.regress_fixtures(pair_code, side=side)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/demo/reset")
def reset():
    return svc.reset_demo()
