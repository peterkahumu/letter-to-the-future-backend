#!/usr/bin/env python
"""
Run one scheduler pass and exit.

In production the pass runs hourly. Locally you don't want to wait an hour to
see whether a letter goes out, so:

    python scripts/run_scheduler.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.core.logging import configure_logging  # noqa: E402
from app.services.scheduler import check_and_send_letters  # noqa: E402


async def main() -> None:
    settings = get_settings()
    configure_logging(debug=settings.debug)
    print(f"Database: {settings.async_database_url.split('@')[-1]}")
    print(f"Mail:     {settings.email_provider}")
    await check_and_send_letters()


if __name__ == "__main__":
    asyncio.run(main())
