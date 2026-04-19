"""Auth dependencies for FastAPI routes."""

from __future__ import annotations

import uuid

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openboek.auth.models import User
from openboek.db import get_session
from openboek.entities.models import Entity, EntityAccess


async def get_current_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> User:
    """Return the authenticated user or raise 401."""
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        uid = uuid.UUID(user_id) if isinstance(user_id, str) else user_id
    except (ValueError, AttributeError):
        raise HTTPException(status_code=401, detail="Invalid session")
    result = await session.execute(select(User).where(User.id == uid))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return user


async def get_entity_for_user(
    entity_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Entity:
    """Return the entity if the user has access, else 403."""
    result = await session.execute(select(Entity).where(Entity.id == entity_id))
    entity = result.scalar_one_or_none()
    if entity is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    # Check access: user owns the entity OR has an access entry
    if entity.owner_user_id == user.id:
        return entity
    access = await session.execute(
        select(EntityAccess).where(
            EntityAccess.user_id == user.id,
            EntityAccess.entity_id == entity_id,
        )
    )
    if access.scalar_one_or_none() is None:
        raise HTTPException(status_code=403, detail="No access to this entity")
    return entity
