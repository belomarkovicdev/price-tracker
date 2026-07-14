"""Scraper package. Importing it registers all available scrapers."""

from .base import Scraper, build_scraper, register, BlockedError  # noqa: F401
from . import polovni  # noqa: F401  (registers polovniautomobili)
from . import kleinanzeigen  # noqa: F401  (registers kleinanzeigen)

# To add a site later: create price_tracker/scrapers/<site>.py with a
# @register("<site>") Scraper subclass, then import it here.
