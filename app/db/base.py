from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared declarative base. Phase 1.2 will define models that inherit
    from this (Opportunity, User, Application, etc.)."""

    pass
