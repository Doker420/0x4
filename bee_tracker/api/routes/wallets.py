"""Wallet, xpub and portfolio endpoints."""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from api.deps import auth, db
from chains.registry import family_of, get_chain
from core.config import XPUB_DEFAULT_GAP, limit_for
from core.security import UnsafeInput, validate_address, validate_xpub
from core.database import LimitExceeded
from services.portfolio import build_portfolio, portfolio_to_csv, sync_xpub

router = APIRouter(tags=["wallets"])


class WalletIn(BaseModel):
    label: str = Field(max_length=64)
    chain: str
    address: str


class XpubIn(BaseModel):
    label: str = Field(max_length=64)
    chain: str
    xpub: str = Field(description="Extended PUBLIC key (xpub/ypub/zpub) only")
    gap: int = Field(default=XPUB_DEFAULT_GAP, ge=1, le=500)


@router.get("/portfolio")
async def get_portfolio(
    snapshot: bool = False,
    hide_empty: bool = True,
    user: dict = Depends(auth),
):
    return await build_portfolio(db, user["id"], snapshot, hide_empty)


@router.get("/portfolio.csv", response_class=PlainTextResponse)
async def get_portfolio_csv(user: dict = Depends(auth)):
    p = await build_portfolio(db, user["id"], hide_empty=False)
    return PlainTextResponse(
        portfolio_to_csv(p),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=bee_portfolio.csv"},
    )


@router.get("/portfolio/history")
async def get_history(days: int = Query(30, ge=1, le=365), user: dict = Depends(auth)):
    return {"history": db.portfolio_history(user["id"], days)}


@router.get("/wallets")
async def list_wallets(user: dict = Depends(auth)):
    return {"wallets": db.get_wallets(user["id"])}


@router.post("/wallets", status_code=201)
async def add_wallet(body: WalletIn, user: dict = Depends(auth)):
    if not get_chain(body.chain):
        raise HTTPException(400, f"Unknown chain '{body.chain}'")
    try:
        addr = validate_address(body.chain, body.address, family_of(body.chain))
        wid = db.add_wallet(user["id"], body.label, body.chain, addr)
    except UnsafeInput as e:
        raise HTTPException(400, str(e))
    except LimitExceeded as e:
        raise HTTPException(402, str(e))
    return {"wallet_id": wid, "address": addr}


@router.delete("/wallets/{wallet_id}")
async def delete_wallet(wallet_id: int, user: dict = Depends(auth)):
    if not db.delete_wallet(user["id"], wallet_id):
        raise HTTPException(404, "Wallet not found")
    return {"deleted": wallet_id}


@router.get("/xpubs")
async def list_xpubs(user: dict = Depends(auth)):
    return {"xpubs": [
        {**x, "xpub": x["xpub"][:16] + "…" + x["xpub"][-6:]}
        for x in db.get_xpubs(user["id"])
    ]}


@router.post("/xpubs", status_code=201)
async def add_xpub(body: XpubIn, user: dict = Depends(auth)):
    """Register an extended PUBLIC key and derive its addresses.

    Extended private keys and seed phrases are rejected with HTTP 400.
    """
    if not get_chain(body.chain):
        raise HTTPException(400, f"Unknown chain '{body.chain}'")
    cap = limit_for(user["tariff"], "gap")
    gap = min(body.gap, cap)
    try:
        xpub = validate_xpub(body.xpub)
        xid = db.add_xpub(user["id"], body.label, body.chain, xpub, gap)
        added = await sync_xpub(db, user["id"], xid, gap)
    except UnsafeInput as e:
        raise HTTPException(400, str(e))
    except LimitExceeded as e:
        raise HTTPException(402, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return {"xpub_id": xid, "addresses_added": added, "gap": gap}


@router.post("/xpubs/{xpub_id}/sync")
async def resync_xpub(
    xpub_id: int,
    gap: int = Query(None, ge=1, le=500),
    user: dict = Depends(auth),
):
    """Re-derive addresses, e.g. after the wallet used more of the gap."""
    try:
        added = await sync_xpub(db, user["id"], xpub_id, gap)
    except LimitExceeded as e:
        raise HTTPException(402, str(e))
    return {"addresses_added": added}


@router.get("/usage")
async def usage(user: dict = Depends(auth)):
    return db.usage(user["id"])
