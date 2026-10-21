"""Market timestamps must not depend on the application host's timezone."""

from datetime import datetime, timedelta, timezone

MARKET_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def market_timestamp(value: str, fmt: str = "%Y-%m-%d") -> int | None:
    if not value:
        return None
    try:
        return int(datetime.strptime(str(value).strip(), fmt).replace(tzinfo=MARKET_TZ).timestamp())
    except (TypeError, ValueError, OverflowError):
        return None
