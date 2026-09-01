"""Punkt wejścia aplikacji FastAPI."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from zibicom import __version__, intake
from zibicom.config import get_settings
from zibicom.db import dispose_engine, get_session, get_sessionmaker
from zibicom.olx import dispose_http_client
from zibicom.routers import router as intake_router
from zibicom.web.routes import router as web_router

logger = logging.getLogger(__name__)

# Referencje do zadań w tle uruchomionych przez `lifespan`, żeby ich nie
# zebrał GC w trakcie działania (asyncio.create_task trzyma tylko słabą
# referencję - bez tego zbioru zadanie mogłoby zniknąć w połowie, patrz
# ostrzeżenie w dokumentacji asyncio.create_task).
_background_tasks: set[asyncio.Task[None]] = set()


async def _run_startup_reconciler() -> None:
    """Odświeża statusy oczekujących ogłoszeń OLX w tle, zaraz po starcie.

    OLX aktywuje ogłoszenia asynchronicznie - `POST /adverts` zwraca status
    przejściowy, a `active` pojawia się dopiero po jakimś czasie
    (`intake.sync_pending_listings`). Bez tego przebiegu listing zostaje w
    bazie jako 'pending', dopóki ktoś ręcznie nie kliknie "Odśwież statusy" -
    ten reconciler przy starcie skraca to okno.

    Uruchamiane przez `asyncio.create_task` w `lifespan`, NIE `await` -
    start aplikacji nie może czekać na odpowiedź OLX (może trwać sekundy,
    a przy awarii OLX - do timeoutu). Własna sesja bazodanowa (nie sesja
    żądania, bo żadnego żądania HTTP jeszcze nie ma).

    Łapie WSZYSTKIE wyjątki i tylko je loguje - `intake.sync_pending_listings`
    już samo obsługuje błędy pojedynczych listingów, ale brak autoryzacji
    OLX (token nigdy nie uzyskany), timeout połączenia czy niedostępność API
    nie mogą przerwać startu aplikacji. Gdyby to zadanie nie było
    izolowane, wygasły/brakujący token OLX uniemożliwiłby uruchomienie
    aplikacji, a tym samym uniemożliwiłby też naprawę tej autoryzacji przez
    interfejs (endpoint OAuth też wymaga działającej aplikacji).

    Ograniczenie: przy `uvicorn --reload` `lifespan` odpala się ponownie po
    każdej zmianie pliku, więc to zadanie też. Akceptowalne w developmencie
    (rzadkie, tanie zapytania); gdyby okazało się uciążliwe, dodać
    przełącznik w konfiguracji (np. wyłączenie w `app_env == "development"`).

    Ten reconciler NIE zastępuje przycisku "Odśwież statusy OLX" w
    interfejsie - statusy zmieniają się przez cały czas działania aplikacji
    (moderacja z opóźnieniem, wygaśnięcie, ręczne zdjęcie), nie tylko przy
    starcie, więc przycisk zostaje jako ręczna synchronizacja na żądanie.
    """
    try:
        async with get_sessionmaker()() as session:
            await intake.sync_pending_listings(session)
    except Exception:
        logger.exception(
            "Reconciler statusów OLX przy starcie aplikacji nie powiódł się."
        )


class HealthResponse(BaseModel):
    """Odpowiedź endpointu /health.

    Attributes:
        status: Ogólny stan aplikacji.
        version: Wersja aplikacji.
        environment: Nazwa środowiska z konfiguracji.
        database: Stan połączenia z bazą danych.
        detail: Komunikat błędu, gdy baza jest niedostępna.
    """

    status: Literal["ok", "degraded"]
    version: str
    environment: str
    database: Literal["ok", "unavailable"]
    detail: str | None = Field(default=None)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Zarządza cyklem życia aplikacji.

    Po starcie wystawia reconciler statusów OLX jako zadanie w tle
    (`_run_startup_reconciler`) - patrz jej docstring o tym, dlaczego to
    `asyncio.create_task`, nie `await`, i dlaczego łapie wszystkie wyjątki.

    Args:
        app: Instancja aplikacji FastAPI.

    Yields:
        Sterowanie na czas działania aplikacji.
    """
    task = asyncio.create_task(_run_startup_reconciler())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    yield
    await dispose_engine()
    await dispose_http_client()


app = FastAPI(
    title="zibicom-olx",
    version=__version__,
    summary="Synchronizacja inwentarza sklepu z ogłoszeniami OLX.",
    lifespan=lifespan,
)

app.include_router(intake_router)
app.include_router(web_router)


@app.get("/health", response_model=HealthResponse, tags=["monitoring"])
async def health(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    """Sprawdza dostępność aplikacji i bazy danych.

    Args:
        session: Sesja bazy danych wstrzykiwana przez FastAPI.

    Returns:
        Odpowiedź 200, gdy baza odpowiada, albo 503 w przeciwnym razie.
    """
    settings = get_settings()
    try:
        await session.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        body = HealthResponse(
            status="degraded",
            version=__version__,
            environment=settings.app_env,
            database="unavailable",
            detail=str(exc),
        )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=body.model_dump(),
        )

    body = HealthResponse(
        status="ok",
        version=__version__,
        environment=settings.app_env,
        database="ok",
    )
    return JSONResponse(status_code=status.HTTP_200_OK, content=body.model_dump())
