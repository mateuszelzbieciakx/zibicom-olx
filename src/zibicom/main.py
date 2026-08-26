"""Punkt wejscia aplikacji FastAPI."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from zibicom import __version__
from zibicom.config import get_settings
from zibicom.db import dispose_engine, get_session
from zibicom.olx import dispose_http_client
from zibicom.routers import router as intake_router
from zibicom.web.routes import router as web_router


class HealthResponse(BaseModel):
    """Odpowiedz endpointu /health.

    Attributes:
        status: Ogolny stan aplikacji.
        version: Wersja aplikacji.
        environment: Nazwa srodowiska z konfiguracji.
        database: Stan polaczenia z baza danych.
        detail: Komunikat bledu, gdy baza jest niedostepna.
    """

    status: Literal["ok", "degraded"]
    version: str
    environment: str
    database: Literal["ok", "unavailable"]
    detail: str | None = Field(default=None)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Zarzadza cyklem zycia aplikacji.

    Args:
        app: Instancja aplikacji FastAPI.

    Yields:
        Sterowanie na czas dzialania aplikacji.
    """
    yield
    await dispose_engine()
    await dispose_http_client()


app = FastAPI(
    title="zibicom-olx",
    version=__version__,
    summary="Synchronizacja inwentarza sklepu z ogloszeniami OLX.",
    lifespan=lifespan,
)

app.include_router(intake_router)
app.include_router(web_router)


@app.get("/health", response_model=HealthResponse, tags=["monitoring"])
async def health(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    """Sprawdza dostepnosc aplikacji i bazy danych.

    Args:
        session: Sesja bazy danych wstrzykiwana przez FastAPI.

    Returns:
        Odpowiedz 200, gdy baza odpowiada, albo 503 w przeciwnym razie.
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
