import argparse
import asyncio
import getpass
import sys
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.core.config import settings
from app.core.password import hash_password
from app.db.session import async_session_factory, init_db
from app.models.user import User, UserRole


async def create_admin(username: str, password: str, email: str = None) -> None:
    """Create a new system administrator account directly via CLI."""
    await init_db()
    async with async_session_factory() as session:
        # Check existing username
        stmt = select(User).where(func.lower(User.username) == username.lower())
        existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing:
            print(f"[ERROR] User with username '{username}' already exists (ID: {existing.id}, Role: {existing.role}).")
            sys.exit(1)

        if email:
            stmt_email = select(User).where(func.lower(User.email) == email.lower())
            existing_email = (await session.execute(stmt_email)).scalar_one_or_none()
            if existing_email:
                print(f"[ERROR] User with email '{email}' already exists.")
                sys.exit(1)

        pwd_hash = hash_password(password)
        admin_user = User(
            username=username.strip(),
            email=email.strip().lower() if email else None,
            password_hash=pwd_hash,
            role=UserRole.ADMIN,
            is_active=True,
            token_version=1,
        )
        session.add(admin_user)
        await session.commit()
        await session.refresh(admin_user)
        print(f"[SUCCESS] System administrator '{admin_user.username}' (ID: {admin_user.id}) created successfully.")


async def promote_user(username: str) -> None:
    """Promote an existing user to system administrator role."""
    await init_db()
    async with async_session_factory() as session:
        stmt = select(User).where(func.lower(User.username) == username.lower())
        user = (await session.execute(stmt)).scalar_one_or_none()
        if not user:
            print(f"[ERROR] User '{username}' not found.")
            sys.exit(1)

        user.role = UserRole.ADMIN
        user.updated_at = datetime.now(timezone.utc)
        await session.commit()
        print(f"[SUCCESS] User '{username}' promoted to '{UserRole.ADMIN}'.")


async def list_users() -> None:
    """List all accounts and roles registered in the database."""
    await init_db()
    async with async_session_factory() as session:
        stmt = select(User).order_by(User.created_at.asc())
        users = (await session.execute(stmt)).scalars().all()
        print(f"\nTotal users: {len(users)}")
        print("-" * 75)
        print(f"{'ID':<26} {'USERNAME':<18} {'ROLE':<8} {'ACTIVE':<8} {'EMAIL'}")
        print("-" * 75)
        for u in users:
            print(f"{u.id:<26} {u.username:<18} {u.role:<8} {str(u.is_active):<8} {u.email or ''}")
        print("-" * 75 + "\n")


def main():
    parser = argparse.ArgumentParser(description="AgentStudio-RAG Administrator CLI Management Tool")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # create
    p_create = subparsers.add_parser("create", help="Create a new administrator account")
    p_create.add_argument("--username", "-u", required=True, help="Administrator username")
    p_create.add_argument("--password", "-p", default=None, help="Administrator password (if omitted, prompts interactively)")
    p_create.add_argument("--email", "-e", default=None, help="Optional email address")

    # promote
    p_promote = subparsers.add_parser("promote", help="Promote an existing user to administrator")
    p_promote.add_argument("--username", "-u", required=True, help="Target username to promote")

    # list
    subparsers.add_parser("list", help="List all registered accounts")

    args = parser.parse_args()

    if args.command == "create":
        pwd = args.password
        if not pwd:
            pwd = getpass.getpass("Enter administrator password: ")
            if not pwd:
                print("[ERROR] Password cannot be empty.")
                sys.exit(1)
        asyncio.run(create_admin(args.username, pwd, args.email))
    elif args.command == "promote":
        asyncio.run(promote_user(args.username))
    elif args.command == "list":
        asyncio.run(list_users())


if __name__ == "__main__":
    main()
