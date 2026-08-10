import datetime as dt

from sqlalchemy import DateTime, func
from sqlalchemy.orm import Mapped, mapped_column


class TimestampMixin:
    """created_at / updated_at columns, set by the database, not the app.

    Using server_default/onupdate (rather than Python-side defaults) means
    the timestamp is correct even for rows inserted outside the ORM (e.g.
    a raw SQL backfill) and avoids clock-skew issues across app instances.
    """

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
