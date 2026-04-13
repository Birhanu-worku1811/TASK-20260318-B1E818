from app.domain.services import seed_admin
from app.infra.db import Base, SessionLocal, engine


def main() -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        admin = seed_admin(db)
        print(f"Seeded admin: {admin.username} ({admin.id})")


if __name__ == "__main__":
    main()
