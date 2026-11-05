import base64
import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.config import settings
from app.models.user import User, UserRole


def generate_server_token(
    user_id: str,
    username: Optional[str] = None,
    role: Optional[str] = None,
    token_version: int = 1,
    secret: Optional[str] = None,
    expires_in: int = 86400,
) -> str:
    """
    Generate a tamper-proof cryptographically signed token for Server deployment mode.
    Format: st_<base64_payload>.<hmac_sha256_signature>
    Payload includes: sub, username, role, token_version, iat, exp.
    """
    signing_secret = secret or settings.SERVER_AUTH_SECRET
    payload: Dict[str, Any] = {
        "sub": user_id,
        "username": username or "",
        "role": role or UserRole.USER,
        "token_version": token_version,
        "iat": int(time.time()),
        "exp": int(time.time()) + expires_in,
    }
    payload_json = json.dumps(payload, separators=(",", ":"))
    payload_b64 = base64.urlsafe_b64encode(payload_json.encode("utf-8")).decode("utf-8").rstrip("=")
    sig = hmac.new(
        signing_secret.encode("utf-8"),
        payload_b64.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"st_{payload_b64}.{sig}"


def verify_server_token_payload(token: str, secret: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Verify cryptographically signed token in Server mode.
    Returns decoded payload dictionary if valid and unexpired; returns None if invalid, forged, or expired.
    Fails closed: arbitrary strings without a valid signature will NEVER be accepted.
    """
    if not token or not token.startswith("st_"):
        return None

    raw_token = token[3:]
    parts = raw_token.split(".")
    if len(parts) != 2:
        return None

    payload_b64, signature = parts
    signing_secret = secret or settings.SERVER_AUTH_SECRET

    # Recompute HMAC signature
    expected_sig = hmac.new(
        signing_secret.encode("utf-8"),
        payload_b64.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(signature, expected_sig):
        return None

    # Decode and check expiry
    try:
        padded_b64 = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload_bytes = base64.urlsafe_b64decode(padded_b64.encode("utf-8"))
        payload_data = json.loads(payload_bytes.decode("utf-8"))

        if payload_data.get("exp", 0) < int(time.time()):
            return None

        return payload_data
    except Exception:
        return None


def verify_server_token(token: str, secret: Optional[str] = None) -> Optional[str]:
    """
    Convenience backward-compatible validator.
    Returns user_id (sub) if valid and unexpired; otherwise None.
    """
    payload = verify_server_token_payload(token, secret=secret)
    if not payload:
        return None
    sub = payload.get("sub")
    return str(sub) if sub else None


async def get_or_create_local_user(db: AsyncSession) -> User:
    """
    Ensure the default local user exists in the users table for Local deployment mode.
    """
    stmt = select(User).where(User.id == settings.DEFAULT_LOCAL_USER_ID)
    user = (await db.execute(stmt)).scalar_one_or_none()
    if not user:
        user = User(
            id=settings.DEFAULT_LOCAL_USER_ID,
            username="local_admin",
            email="local@agentstudio.local",
            password_hash="!local_disabled_pwd",
            role=UserRole.ADMIN,
            is_active=True,
            token_version=1,
        )
        db.add(user)
        try:
            await db.commit()
            await db.refresh(user)
        except Exception:
            await db.rollback()
            # If created concurrently
            user = (await db.execute(stmt)).scalar_one_or_none()
            if not user:
                raise
    return user


async def get_current_user(
    db: AsyncSession = Depends(get_db),
    authorization: Optional[str] = Header(None, alias="Authorization"),
) -> User:
    """
    FastAPI dependency that returns the authenticated User entity.

    1. Local Mode (settings.DEPLOYMENT_MODE == "local"):
       - Always returns the default local admin user (usr_local_default).
       - Zero friction, no credentials needed.
    2. Server Mode (settings.DEPLOYMENT_MODE == "server"):
       - Strictly validates Bearer token.
       - Validates signature, expiration, active status, and token_version.
       - Fails closed with HTTP 401 Unauthorized.
    """
    if settings.DEPLOYMENT_MODE == "local":
        return await get_or_create_local_user(db)

    # Server mode: must fail closed if token is missing or invalid
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required in server deployment mode. Please provide Authorization Bearer token.",
        )

    raw_token = authorization.split(" ", 1)[1].strip()
    payload = verify_server_token_payload(raw_token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired authentication token. Unauthenticated or forged credentials are rejected.",
        )

    user_id = payload.get("sub")
    token_ver = payload.get("token_version")

    stmt = select(User).where(User.id == user_id)
    user = (await db.execute(stmt)).scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User associated with this token no longer exists.",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account has been deactivated. Please contact system administrator.",
        )

    if token_ver is not None and user.token_version != token_ver:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been invalidated or revoked. Please log in again.",
        )

    return user


async def get_current_user_id(
    db: AsyncSession = Depends(get_db),
    authorization: Optional[str] = Header(None, alias="Authorization"),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
) -> str:
    """
    Unified User Identity Dependency for AgentStudio-RAG.

    Supports two deployment modes:
    1. Local Mode (settings.DEPLOYMENT_MODE == "local"):
       - Single-user local setup, zero-barrier usage.
       - Defaults to settings.DEFAULT_LOCAL_USER_ID ('usr_local_default').
       - In production, rejects arbitrary client-supplied identities via X-User-Id.
       - Legacy test override only permitted if settings.ALLOW_TEST_USER_OVERRIDE is enabled.
    2. Server Mode (settings.DEPLOYMENT_MODE == "server"):
       - Multi-user team setup.
       - Strictly extracts authenticated user identity from a valid cryptographically signed Bearer token.
       - Arbitrary strings, forged tokens, or missing credentials strictly raise HTTP 401 Unauthorized (Fail Closed).
       - Never falls back to Local User in Server mode.
    """
    if settings.DEPLOYMENT_MODE == "local":
        if getattr(settings, "ALLOW_TEST_USER_OVERRIDE", False) and x_user_id and x_user_id.strip():
            return x_user_id.strip()
        return settings.DEFAULT_LOCAL_USER_ID

    # Server mode: delegate to full user verification
    user = await get_current_user(db=db, authorization=authorization)
    return user.id
