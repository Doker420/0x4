"""BEE Tracker whitelabel REST API."""
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import admin, wallets, webhooks
from chains.base import close_session
from chains.registry import chains_by_family, list_chains, total_chains

logging.basicConfig(level=logging.INFO)

DESCRIPTION = """
Watch-only multi-chain portfolio tracking and transaction monitoring.

**Security model:** this API accepts public addresses and extended *public*
keys (xpub/ypub/zpub) only. Seed phrases and private keys are rejected with
HTTP 400 and are never stored, logged or transmitted. Nothing in this system
is capable of moving funds.

Authenticate with the `X-API-Key` header.
"""

app = FastAPI(
    title="BEE Tracker API",
    version="1.0.0",
    description=DESCRIPTION,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["X-API-Key", "Content-Type"],
)

app.include_router(wallets.router)
app.include_router(webhooks.router)
app.include_router(admin.router)


@app.get("/health", tags=["meta"])
async def health():
    return {"ok": True, "chains": total_chains(), "watch_only": True}


@app.get("/chains", tags=["meta"])
async def chains():
    return {
        "total": total_chains(),
        "chains": list_chains(),
        "by_family": chains_by_family(),
    }


@app.on_event("shutdown")
async def shutdown():
    await close_session()
