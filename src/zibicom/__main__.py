"""Uruchamianie serwera: python -m zibicom.

Osobny entrypoint istnieje z powodu Windowsa: uvicorn domyslnie wybiera tam
ProactorEventLoop, na ktorym psycopg 3 nie potrafi pracowac. Zamiast polegac
na przestarzalym API polityk petli (usuwane w Pythonie 3.16), wymuszamy
loop_factory przy starcie.
"""

import asyncio
import sys
from collections.abc import Callable

import uvicorn

from zibicom.config import get_settings


def _loop_factory(
    config: uvicorn.Config,
) -> Callable[[], asyncio.AbstractEventLoop] | None:
    """Dobiera fabryke petli zdarzen odpowiednia dla platformy.

    Args:
        config: Konfiguracja uvicorna, z ktorej bierzemy domyslna fabryke.

    Returns:
        SelectorEventLoop na Windowsie, w przeciwnym razie wybor uvicorna.
    """
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop
    return config.get_loop_factory()


def main() -> None:
    """Startuje serwer HTTP na podstawie ustawien aplikacji."""
    settings = get_settings()
    config = uvicorn.Config(
        "zibicom.main:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
    )
    server = uvicorn.Server(config)
    asyncio.run(server.serve(), loop_factory=_loop_factory(config))


if __name__ == "__main__":
    main()
