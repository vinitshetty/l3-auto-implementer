"""API endpoints for repo profiles — view and manage cached repo understanding."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import RepoProfile
from app.schemas import RepoProfileResponse

router = APIRouter(tags=["repo-profiles"])


@router.get("/repo-profiles", response_model=list[RepoProfileResponse])
async def list_repo_profiles(db: AsyncSession = Depends(get_db)):
    """List all cached repo profiles."""
    result = await db.execute(
        select(RepoProfile).order_by(RepoProfile.updated_at.desc())
    )
    return result.scalars().all()


@router.get("/repo-profiles/{profile_id}", response_model=RepoProfileResponse)
async def get_repo_profile(profile_id: str, db: AsyncSession = Depends(get_db)):
    """Get a specific repo profile."""
    profile = await db.get(RepoProfile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile


@router.delete("/repo-profiles/{profile_id}")
async def delete_repo_profile(profile_id: str, db: AsyncSession = Depends(get_db)):
    """Delete a cached profile (will be regenerated on next session)."""
    profile = await db.get(RepoProfile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    await db.delete(profile)
    await db.commit()
    return {"status": "deleted", "repo_url": profile.repo_url}
