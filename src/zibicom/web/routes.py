"""Router zwracajacy HTML dla interfejsu pracownika.

Rownolegly do JSON API w `zibicom.routers` - te same funkcje serwisowe,
inna reprezentacja. Logika biznesowa nie moze istniec w dwoch kopiach.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from zibicom import intake, olx
from zibicom.db import get_session, get_sessionmaker

router = APIRouter(prefix="/ui", tags=["ui"])
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
logger = logging.getLogger(__name__)

SessionDep = Annotated[AsyncSession, Depends(get_session)]

# Partie, ktorych ekstrakcja aktualnie biegnie w tle (guard idempotencji dla
# POST /batches/{id}/extract - zyje tylko w pamieci procesu, patrz komentarz
# przy _run_extraction o utracie postepu przy restarcie).
_running: set[int] = set()


async def _card(
    request: Request,
    session: AsyncSession,
    item: intake.IntakeItemView,
    error: str | None = None,
) -> HTMLResponse:
    """Renderuje fragment karty pozycji.

    Args:
        request: Zadanie HTTP wymagane przez Jinja2Templates.
        session: Sesja bazodanowa (do slownika platform).
        item: Widok pozycji do wyswietlenia.
        error: Komunikat bledu domenowego do pokazania w karcie.

    Returns:
        Fragment HTML karty, zawsze ze statusem 200 - HTMX podmienia DOM
        wylacznie przy 2xx, wiec blad walidacji musi przyjsc jako poprawna
        odpowiedz z trescia bledu, nie jako HTTPException.
    """
    return templates.TemplateResponse(
        request=request,
        name="partials/item_card.html",
        context={
            "item": item,
            "error": error,
            "platforms": await intake.list_platforms(session),
        },
    )


def _progress_context(batch_id: int, done: int, total: int) -> dict[str, object]:
    """Buduje kontekst dla `partials/batch_progress.html`."""
    return {
        "batch_id": batch_id,
        "done": done,
        "total": total,
        "percent": round(done / total * 100) if total else 0,
    }


async def _batch_status(session: AsyncSession, batch_id: int) -> str:
    """Zwraca surowy status partii (`intake_batch.status`)."""
    return (
        await session.execute(
            text("SELECT status::TEXT FROM intake_batch WHERE id = :batch_id"),
            {"batch_id": batch_id},
        )
    ).scalar_one()


async def _run_extraction(batch_id: int) -> None:
    """Uruchamia `extract_batch` w tle, na wlasnej sesji DB.

    Wlasna sesja, bo ta z zadania HTTP jest zamykana, gdy tylko odpowiedz
    zostanie wyslana - `extract_batch` musi dzialac dlugo po tym momencie.

    Ograniczenie zaakceptowane dla jednego operatora: restart procesu w
    trakcie ekstrakcji gubi jej postep (brak trwalej kolejki zadan - `_running`
    zyje tylko w pamieci). Docelowo: prawdziwa kolejka zadan (np. arq/celery)
    zamiast BackgroundTasks.
    """
    try:
        async with get_sessionmaker()() as session:
            await intake.extract_batch(session, batch_id)
    except Exception:
        logger.exception("Ekstrakcja partii %s w tle nie powiodla sie.", batch_id)
    finally:
        _running.discard(batch_id)


@router.get("/batches", response_class=HTMLResponse)
async def batches(request: Request, session: SessionDep) -> HTMLResponse:
    """Renderuje liste wszystkich partii, od najnowszej."""
    return templates.TemplateResponse(
        request=request,
        name="batches.html",
        context={"batches": await intake.list_batches(session)},
    )


@router.post("/batches")
async def create_batch(
    request: Request,
    session: SessionDep,
    files: Annotated[
        list[UploadFile], File(description="Zdjecia w kolejnosci wgrania.")
    ],
) -> HTMLResponse:
    """Tworzy nowa partie z plikow wgranych przez formularz w przegladarce.

    Zwykly POST/Redirect/Get (bez HTMX): po sukcesie 303 See Other na
    `/ui/batches/{batch_id}`, zeby odswiezenie strony nie powtorzylo uploadu
    (307 by to zrobilo). Blad domenowy re-renderuje liste partii z
    komunikatem, statusem 200 - nie ma dokad przekierowac, bo partia nie
    powstala.
    """
    payload = [(f.filename, await f.read()) for f in files]
    try:
        batch_id = await intake.create_batch(session, payload)
    except intake.IntakeError as exc:
        return templates.TemplateResponse(
            request=request,
            name="batches.html",
            context={"batches": await intake.list_batches(session), "error": str(exc)},
        )
    return RedirectResponse(f"/ui/batches/{batch_id}", status_code=303)


@router.get("/batches/{batch_id}", response_class=HTMLResponse)
async def batch_detail(
    request: Request, batch_id: int, session: SessionDep
) -> HTMLResponse:
    """Renderuje pozycje poczekalni dla wskazanej partii."""
    photo_count = (
        await session.execute(
            text("SELECT COUNT(*) FROM intake_photo WHERE batch_id = :batch_id"),
            {"batch_id": batch_id},
        )
    ).scalar_one()
    items = await intake.list_items(session, batch_id)
    extracting = batch_id in _running
    context: dict[str, object] = {
        "batch_id": batch_id,
        "photo_count": photo_count,
        "items": items,
        "platforms": await intake.list_platforms(session),
        "extracting": extracting,
    }
    if extracting:
        done, total = await intake.extraction_progress(session, batch_id)
        context.update(_progress_context(batch_id, done, total))
    elif not items and await _batch_status(session, batch_id) == "failed":
        context["error"] = "Poprzednia proba rozpoznania nie powiodla sie."
    return templates.TemplateResponse(
        request=request, name="batch_detail.html", context=context
    )


@router.post("/batches/{batch_id}/extract", response_class=HTMLResponse)
async def start_extraction(
    request: Request,
    batch_id: int,
    background_tasks: BackgroundTasks,
    session: SessionDep,
) -> HTMLResponse:
    """Wystawia zadanie rozpoznania partii w tle i zwraca fragment postepu.

    Guard idempotencji: jesli partia ma juz pozycje albo ekstrakcja juz
    biegnie (`_running`), NIE wystawia drugiego zadania - zwraca biezacy
    fragment (karty albo pasek postepu), zeby dwuklikniecie/podwojne
    zdarzenie HTMX nie puscilo rownoleglego przebiegu.
    """
    items = await intake.list_items(session, batch_id)
    if items:
        return templates.TemplateResponse(
            request=request,
            name="partials/batch_items.html",
            context={"items": items, "platforms": await intake.list_platforms(session)},
        )

    done, total = await intake.extraction_progress(session, batch_id)
    if batch_id not in _running:
        _running.add(batch_id)
        background_tasks.add_task(_run_extraction, batch_id)
    return templates.TemplateResponse(
        request=request,
        name="partials/batch_progress.html",
        context=_progress_context(batch_id, done, total),
    )


@router.get("/batches/{batch_id}/progress", response_class=HTMLResponse)
async def batch_extraction_progress(
    request: Request, batch_id: int, session: SessionDep
) -> HTMLResponse:
    """Fragment polingowany przez HTMX co 2s, dopoki ekstrakcja biegnie.

    Zakonczona (pozycje juz istnieja) -> karty pozycji, bez `hx-trigger`,
    wiec poling zatrzymuje sie sam. Nadal w toku -> pasek postepu z tym
    samym `hx-get` (poling siebie). Ani jedno, ani drugie (ekstrakcja
    padla przed utworzeniem pozycji) -> przycisk "Rozpoznaj" z komunikatem
    bledu.
    """
    items = await intake.list_items(session, batch_id)
    if items:
        return templates.TemplateResponse(
            request=request,
            name="partials/batch_items.html",
            context={"items": items, "platforms": await intake.list_platforms(session)},
        )
    if batch_id in _running:
        done, total = await intake.extraction_progress(session, batch_id)
        return templates.TemplateResponse(
            request=request,
            name="partials/batch_progress.html",
            context=_progress_context(batch_id, done, total),
        )
    error = None
    if await _batch_status(session, batch_id) == "failed":
        error = (
            "Rozpoznawanie partii nie powiodlo sie. Sprawdz logi i sprobuj ponownie."
        )
    return templates.TemplateResponse(
        request=request,
        name="partials/batch_extract_button.html",
        context={"batch_id": batch_id, "error": error},
    )


@router.post("/items/{item_id}/save", response_class=HTMLResponse)
async def save_item(
    request: Request,
    item_id: int,
    session: SessionDep,
    title: Annotated[str, Form()] = "",
    price_pln: Annotated[str, Form()] = "",
    condition: Annotated[str, Form()] = "",
    platform_id: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Zapisuje reczna korekte pol pozycji.

    Formularz HTML przysyla wszystkie pola jako tekst (puste = pusty string),
    wiec konwersja na typy domenowe odbywa sie tutaj - `IntakeItemUpdate`
    dostaje juz wartosci wlasciwego typu.
    """
    try:
        price = Decimal(price_pln) if price_pln.strip() else None
    except InvalidOperation:
        current = await intake.get_item(session, item_id)
        return await _card(request, session, current, "Cena musi byc liczba.")

    payload = intake.IntakeItemUpdate(
        title=title.strip() or None,
        price_pln=price,
        condition=condition or None,
        platform_id=int(platform_id) if platform_id.strip() else None,
    )
    try:
        item = await intake.update_item(session, item_id, payload)
    except intake.IntakeError as exc:
        return await _card(
            request, session, await intake.get_item(session, item_id), str(exc)
        )
    return await _card(request, session, item)


@router.post("/items/{item_id}/approve", response_class=HTMLResponse)
async def approve_item(
    request: Request, item_id: int, session: SessionDep
) -> HTMLResponse:
    """Zatwierdza pozycje poczekalni."""
    try:
        item = await intake.approve_item(session, item_id)
    except intake.IntakeError as exc:
        return await _card(
            request, session, await intake.get_item(session, item_id), str(exc)
        )
    return await _card(request, session, item)


@router.post("/items/{item_id}/reject", response_class=HTMLResponse)
async def reject_item(
    request: Request, item_id: int, session: SessionDep
) -> HTMLResponse:
    """Odrzuca pozycje poczekalni."""
    try:
        item = await intake.reject_item(session, item_id)
    except intake.IntakeError as exc:
        return await _card(
            request, session, await intake.get_item(session, item_id), str(exc)
        )
    return await _card(request, session, item)


@router.post("/items/{item_id}/publish", response_class=HTMLResponse)
async def publish_item(
    request: Request, item_id: int, session: SessionDep
) -> HTMLResponse:
    """Publikuje zatwierdzona pozycje na OLX. Operacja nieodwracalna."""
    try:
        item = await intake.publish_item(session, item_id)
    except (intake.IntakeError, olx.OlxError) as exc:
        return await _card(
            request, session, await intake.get_item(session, item_id), str(exc)
        )
    return await _card(request, session, item)
