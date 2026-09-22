"""Admin endpoints — restricted to ADMIN_IDS."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.deps import auth, db
from chains.registry import total_chains
from core.config import ADMIN_IDS

router = APIRouter(prefix="/admin", tags=["admin"])


def require_admin(user: dict = Depends(auth)) -> dict:
    if user["id"] not in ADMIN_IDS:
        raise HTTPException(403, "Admin access required")
    return user


class TariffIn(BaseModel):
    user_id: int
    tariff: str = Field(pattern="^(free|pro|team)$")


class TeamIn(BaseModel):
    name: str = Field(max_length=64)
    owner_id: int


class MemberIn(BaseModel):
    team_id: int
    user_id: int


@router.get("/stats")
async def stats(admin: dict = Depends(require_admin)):
    return {**db.stats(), "chains": total_chains()}


@router.post("/tariff")
async def set_tariff(body: TariffIn, admin: dict = Depends(require_admin)):
    db.ensure_user(body.user_id)
    db.set_tariff(body.user_id, body.tariff)
    return {"user_id": body.user_id, "tariff": body.tariff}


@router.post("/teams", status_code=201)
async def create_team(body: TeamIn, admin: dict = Depends(require_admin)):
    db.ensure_user(body.owner_id)
    return {"team_id": db.create_team(body.name, body.owner_id)}


@router.post("/teams/members")
async def add_member(body: MemberIn, admin: dict = Depends(require_admin)):
    db.ensure_user(body.user_id)
    db.add_team_member(body.team_id, body.user_id)
    return {"team_id": body.team_id, "members": db.team_members(body.team_id)}
