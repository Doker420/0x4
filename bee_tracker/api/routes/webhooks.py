"""Watch address and webhook endpoints."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.deps import auth, db
from chains.registry import family_of, get_chain
from core.database import LimitExceeded
from core.security import UnsafeInput, validate_address, validate_webhook_url

router = APIRouter(tags=["monitoring"])

VALID_EVENTS = {"tx_in", "tx_out", "balance_change"}


class WatchIn(BaseModel):
    label: str = Field(max_length=64)
    chain: str
    address: str
    min_amount_usd: float = Field(default=0, ge=0)
    direction_filter: str = Field(default="both", pattern="^(in|out|both)$")


class WebhookIn(BaseModel):
    url: str
    events: str = Field(
        default="tx_in,tx_out",
        description="Comma-separated: tx_in, tx_out, balance_change",
    )


@router.get("/watches")
async def list_watches(active_only: bool = False, user: dict = Depends(auth)):
    return {"watches": db.get_watches(user["id"], active_only)}


@router.post("/watches", status_code=201)
async def add_watch(body: WatchIn, user: dict = Depends(auth)):
    if not get_chain(body.chain):
        raise HTTPException(400, f"Unknown chain '{body.chain}'")
    try:
        addr = validate_address(body.chain, body.address, family_of(body.chain))
        wid = db.add_watch(user["id"], body.label, body.chain, addr,
                           body.min_amount_usd, body.direction_filter)
    except UnsafeInput as e:
        raise HTTPException(400, str(e))
    except LimitExceeded as e:
        raise HTTPException(402, str(e))
    return {"watch_id": wid}


@router.delete("/watches/{watch_id}")
async def delete_watch(watch_id: int, user: dict = Depends(auth)):
    if not db.delete_watch(user["id"], watch_id):
        raise HTTPException(404, "Watch not found")
    return {"deleted": watch_id}


@router.get("/transactions")
async def list_txs(limit: int = 50, user: dict = Depends(auth)):
    return {"transactions": db.get_txs(user["id"], min(limit, 500))}


@router.get("/webhooks")
async def list_webhooks(user: dict = Depends(auth)):
    return {"webhooks": [
        {k: v for k, v in w.items() if k != "secret"}
        for w in db.get_webhooks(user["id"])
    ]}


@router.post("/webhooks", status_code=201)
async def add_webhook(body: WebhookIn, user: dict = Depends(auth)):
    events = {e.strip() for e in body.events.split(",") if e.strip()}
    unknown = events - VALID_EVENTS
    if unknown:
        raise HTTPException(400, f"Unknown events: {', '.join(unknown)}")
    try:
        url = validate_webhook_url(body.url)
        created = db.add_webhook(user["id"], url, ",".join(sorted(events)))
    except UnsafeInput as e:
        raise HTTPException(400, str(e))
    except LimitExceeded as e:
        raise HTTPException(402, str(e))
    return {
        "webhook_id": created["id"],
        "secret": created["secret"],
        "note": "Store this secret now; it verifies the X-Bee-Signature header "
                "(HMAC-SHA256 of the raw body). It is not shown again.",
    }


@router.delete("/webhooks/{webhook_id}")
async def delete_webhook(webhook_id: int, user: dict = Depends(auth)):
    if not db.delete_webhook(user["id"], webhook_id):
        raise HTTPException(404, "Webhook not found")
    return {"deleted": webhook_id}
