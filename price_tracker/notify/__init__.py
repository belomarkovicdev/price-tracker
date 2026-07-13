"""Notification channels. The engine depends only on the Notifier interface,
so swapping/adding channels (email, Discord, ...) doesn't touch the pipeline."""

from .base import Notifier, Notification  # noqa: F401
from .telegram import TelegramNotifier, LogNotifier  # noqa: F401
