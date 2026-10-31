"""Explicit local administrative provisioning; never accepts password arguments."""

import argparse
import getpass
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.schemas import Credentials
from app.auth.security import hash_password
from app.auth.service import audit
from app.core.config import get_settings
from app.db.session import get_engine
from app.models import User


def main() -> None:
    parser = argparse.ArgumentParser(description="Provision a FinGuard administrator")
    parser.add_argument("--email", required=True)
    parser.add_argument("--confirm-admin", action="store_true")
    parser.add_argument("--rotate-existing-password", action="store_true")
    args = parser.parse_args()
    if not args.confirm_admin:
        parser.error("--confirm-admin is required")
    settings = get_settings()
    if not settings.database_url:
        parser.error("DATABASE_URL is required")
    password = getpass.getpass("New admin password: ")
    confirm = getpass.getpass("Confirm admin password: ")
    if password != confirm:
        parser.error("Passwords do not match")
    request = Credentials(email=args.email, password=password)
    with Session(get_engine(settings.database_url)) as session, session.begin():
        existing = session.scalar(select(User).where(User.email == request.email).with_for_update())
        if existing is not None:
            if existing.role != "admin":
                parser.error("Existing non-admin user cannot be promoted by this script")
            if not args.rotate_existing_password:
                parser.error("Admin already exists; use --rotate-existing-password to rotate")
            existing.password_hash = hash_password(request.password, settings)
            audit(session, "admin.password_rotated", user_id=existing.id)
            print("Administrator password rotated.")
        else:
            user = User(
                id=uuid4(),
                email=request.email,
                password_hash=hash_password(request.password, settings),
                role="admin",
                status="active",
            )
            session.add(user)
            session.flush()
            audit(session, "admin.provisioned", user_id=user.id)
            print("Administrator created.")


if __name__ == "__main__":
    main()
