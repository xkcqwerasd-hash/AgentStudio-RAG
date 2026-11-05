import asyncio
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.auth import generate_server_token, get_current_user
from app.core.config import settings
from app.core.password import (
    dummy_verify_password,
    hash_password,
    needs_rehash,
    verify_password,
)
from app.models.user import User, UserRole
from app.schemas.auth import (
    AuthModeResponse,
    LogoutResponse,
    PasswordChangeRequest,
    PasswordChangeResponse,
    TokenResponse,
    UserLoginRequest,
    UserRegisterRequest,
    UserResponse,
)

router = APIRouter(prefix="/auth", tags=["Authentication & Identity"])


@router.get("/mode", response_model=AuthModeResponse, status_code=status.HTTP_200_OK)
async def get_auth_mode():
    """
    Public discovery endpoint for frontend initialization.
    Returns non-sensitive deployment mode and registration availability.
    """
    return AuthModeResponse(
        deployment_mode=settings.DEPLOYMENT_MODE,
        allow_registration=settings.SERVER_ALLOW_REGISTRATION,
        version="1.3.0",
    )


# Process-level lock to prevent race condition during registration
_registration_lock = asyncio.Lock()


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(
    payload: UserRegisterRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Register a new user account.
    - If SERVER_ALLOW_REGISTRATION is False, returns 403 Forbidden.
    - All publicly registered users receive the standard 'user' role.
    - Public visitors cannot acquire 'admin' privileges via registration.
    - System administrators are initialized and managed via the server-side CLI tool.
    """
    if not settings.SERVER_ALLOW_REGISTRATION:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Public registration is currently disabled by system administrator.",
        )

    norm_username = payload.username.strip()
    norm_email = payload.email.strip().lower() if payload.email else None

    # Check for existing username (case-insensitive)
    stmt_user = select(User).where(func.lower(User.username) == norm_username.lower())
    existing_user = (await db.execute(stmt_user)).scalar_one_or_none()
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Username '{norm_username}' is already taken.",
        )

    # Check for existing email (case-insensitive)
    if norm_email:
        stmt_email = select(User).where(func.lower(User.email) == norm_email.lower())
        existing_email = (await db.execute(stmt_email)).scalar_one_or_none()
        if existing_email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Email '{norm_email}' is already registered.",
            )

    async with _registration_lock:
        role = UserRole.USER
        if settings.AUTO_FIRST_USER_ADMIN:
            count_stmt = select(func.count(User.id))
            user_count = (await db.execute(count_stmt)).scalar() or 0
            if user_count == 0:
                role = UserRole.ADMIN

        pwd_hash = hash_password(payload.password)
        new_user = User(
            username=norm_username,
            email=norm_email,
            password_hash=pwd_hash,
            role=role,
            is_active=True,
            token_version=1,
        )
        db.add(new_user)
        try:
            await db.commit()
            await db.refresh(new_user)
        except Exception as exc:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Registration failed: {str(exc)}",
            ) from exc

    return UserResponse.model_validate(new_user)


@router.post("/login", response_model=TokenResponse, status_code=status.HTTP_200_OK)
async def login(
    payload: UserLoginRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Authenticate user credentials and issue an HMAC-SHA256 signed Bearer token.
    Mitigates user enumeration timing attacks via dummy password verification.
    """
    identifier = payload.username.strip()

    # Match by username or email
    stmt = select(User).where(
        (func.lower(User.username) == identifier.lower()) |
        (func.lower(User.email) == identifier.lower())
    )
    user = (await db.execute(stmt)).scalar_one_or_none()

    if not user:
        dummy_verify_password()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password.",
        )

    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password.",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account has been deactivated. Please contact system administrator.",
        )

    # Transparent password hash upgrade if configured iterations have evolved
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)

    user.last_login_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(user)

    token = generate_server_token(
        user_id=user.id,
        username=user.username,
        role=user.role,
        token_version=user.token_version,
    )

    return TokenResponse(
        access_token=token,
        token_type="bearer",
        expires_in=86400,
        user=UserResponse.model_validate(user),
    )


@router.get("/me", response_model=UserResponse, status_code=status.HTTP_200_OK)
async def get_me(
    current_user: User = Depends(get_current_user),
):
    """
    Get the authenticated user's profile and role.
    """
    return UserResponse.model_validate(current_user)


@router.post("/logout", response_model=LogoutResponse, status_code=status.HTTP_200_OK)
async def logout(
    current_user: User = Depends(get_current_user),
):
    """
    Logout the current session.
    Note on token revocation:
    In stateless HMAC-SHA256 authentication without a centralized stateful cache,
    this endpoint signals client-side discard (revocation_type: client_discard).
    For instant server-side revocation across all devices, change password or rotate token_version.
    """
    return LogoutResponse(
        message=f"User '{current_user.username}' logged out successfully. Please discard the client-side token.",
        revocation_type="client_discard",
    )


@router.post("/password", response_model=PasswordChangeResponse, status_code=status.HTTP_200_OK)
async def change_password(
    payload: PasswordChangeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Change user password.
    Increments token_version to immediately invalidate all existing tokens across all devices.
    """
    if not verify_password(payload.old_password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password verification failed. Please enter your correct old password.",
        )

    current_user.password_hash = hash_password(payload.new_password)
    current_user.token_version += 1
    current_user.updated_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(current_user)

    return PasswordChangeResponse(
        message="Password updated successfully. All existing sessions have been invalidated."
    )
