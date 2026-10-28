"""Optional personal Work domain; it does not run models or schedule reminders."""

from .routes import router
from .store import init_productivity_db

__all__ = ["init_productivity_db", "router"]
