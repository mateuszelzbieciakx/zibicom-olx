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

# Analogiczny guard idempotencji dla POST /batches/{id}/publish-all - drugi
# klik/zdarzenie HTMX, dopoki poprzedni przebieg nie skonczy, nie wystawia
# drugiego zadania w tle (patrz uzasadnienie sekwencyjnosci w
# intake.publish_batch: rownolegle zadania OLX moga trwale uniewaznic
# autoryzacje przez wyscig o rotacje refresh tokenu).
_running_publish: set[int] = set()

# Wynik ostatniego zakonczonego przebiegu `publish_batch`, do jednorazowego
# pokazania w podsumowaniu po tym, jak batch_id zniknie z _running_publish -
# `batch_publish_progress` go odczytuje i usuwa (patrz tamten docstring).
# Zyje tylko w pamieci procesu, tak samo jak _running/_running_publish.
_publish_results: dict[int, intake.BulkPublishResult] = {}


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

    Restart procesu w trakcie ekstrakcji NIE gubi juz zrobionej pracy -
    `intake.extract_batch` jest wznawialna (commituje kazde rozpoznane
    zdjecie i kazdy domkniety egzemplarz na biezaco) - traci sie tylko
    fakt bycia "w toku": `_running` zyje wylacznie w pamieci procesu, wiec
    po restarcie batch_detail pokaze przycisk "Wznow rozpoznawanie"
    zamiast paska postepu, dopoki operator go nie kliknie. Docelowo:
    prawdziwa kolejka zadan (np. arq/celery) zamiast BackgroundTasks, zeby
    wznowienie bylo automatyczne.
    """
    try:
        async with get_sessionmaker()() as session:
            await intake.extract_batch(session, batch_id)
    except Exception:
        logger.exception("Ekstrakcja partii %s w tle nie powiodla sie.", batch_id)
    finally:
        _running.discard(batch_id)


async def _run_publish_batch(batch_id: int) -> None:
    """Uruchamia `publish_batch` w tle, na wlasnej sesji DB.

    Wlasna sesja z tego samego powodu co `_run_extraction`: zadanie HTTP,
    ktore wystawilo to zadanie w tle, juz zakonczylo odpowiedz, a masowa
    publikacja partii moze trwac minuty (150 pozycji x ~1,3s, patrz
    `intake.publish_batch`). Wynik trafia do `_publish_results` PRZED
    usunieciem batch_id z `_running_publish`, zeby poling
    `/publish-progress` nigdy nie zobaczyl "juz nie biegnie" bez gotowego
    wyniku do pokazania.
    """
    try:
        async with get_sessionmaker()() as session:
            _publish_results[batch_id] = await intake.publish_batch(session, batch_id)
    except Exception:
        logger.exception(
            "Masowa publikacja partii %s w tle nie powiodla sie.", batch_id
        )
    finally:
        _running_publish.discard(batch_id)


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
    publishing = batch_id in _running_publish
    publishable_count = sum(
        1 for item in items if item.status in ("pending", "approved")
    )
    batch_status = await _batch_status(session, batch_id)
    context: dict[str, object] = {
        "batch_id": batch_id,
        "photo_count": photo_count,
        "items": items,
        "platforms": await intake.list_platforms(session),
        "extracting": extracting,
        "publishing": publishing,
        "publishable_count": publishable_count,
        "batch_status": batch_status,
        "extraction_error": None,
    }
    if extracting:
        done, total = await intake.extraction_progress(session, batch_id)
        context.update(_progress_context(batch_id, done, total))
        # Kursor dla pierwszego pollu (partials/batch_progress.html): karty
        # do tego id sa juz wyrenderowane na stronie (batch_items.html
        # powyzej) - bez tego pierwszy poling dostalby je jako "nowe" i
        # dopial drugi raz (patrz batch_extraction_progress/after_item_id).
        context["last_item_id"] = items[-1].id if items else 0
    elif publishing:
        done, total = await intake.publish_progress(session, batch_id)
        context.update(_progress_context(batch_id, done, total))
    elif batch_status in ("extracting", "failed"):
        # 'extracting' bez batch_id w `_running` = proces zostal zabity w
        # trakcie (restart, OOM) - zaden Python-owy except nie zdazyl
        # ustawic 'failed'. Wznowienie jest tak samo bezpieczne jak po
        # czystym bledzie (`intake.extract_batch` jest wznawialna).
        context["extraction_error"] = (
            "Rozpoznawanie zostalo przerwane. Wznowienie kontynuuje od "
            "miejsca przerwania (juz rozpoznane zdjecia i zapisane "
            "egzemplarze nie zostana utworzone drugi raz)."
        )
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
    """Wystawia (lub wznawia) rozpoznanie partii w tle i zwraca pasek postepu.

    Guard idempotencji: TYLKO jesli ekstrakcja tej partii juz biegnie w
    tym procesie (`_running`) NIE wystawia drugiego zadania - to jedyny
    warunek, ktory ma teraz znaczenie. `intake.extract_batch` jest
    wznawialna: ponowne wywolanie na juz ukonczonej albo czesciowo
    przetworzonej partii jest bezpieczne samo w sobie (pomija zdjecia z
    wypelnionym ai_raw i egzemplarze juz zapisane), wiec w odroznieniu od
    poprzedniej (atomowej) wersji nie trzeba juz sprawdzac, czy partia ma
    juz pozycje.
    """
    items = await intake.list_items(session, batch_id)
    last_item_id = items[-1].id if items else 0

    if batch_id not in _running:
        _running.add(batch_id)
        background_tasks.add_task(_run_extraction, batch_id)
    done, total = await intake.extraction_progress(session, batch_id)
    context = {"last_item_id": last_item_id}
    context.update(_progress_context(batch_id, done, total))
    return templates.TemplateResponse(
        request=request, name="partials/batch_progress.html", context=context
    )


@router.get("/batches/{batch_id}/progress", response_class=HTMLResponse)
async def batch_extraction_progress(
    request: Request,
    batch_id: int,
    session: SessionDep,
    after_item_id: int = 0,
) -> HTMLResponse:
    """Fragment polingowany przez HTMX co 2s w trakcie ekstrakcji.

    Dopina WYLACZNIE nowo domkniete egzemplarze (id > after_item_id) do
    #batch-items przez OOB append (`hx-swap-oob="beforeend"` -
    `partials/batch_items_append.html`) - NIGDY nie przerenderowywuje
    calej listy, zeby nie skasowac niezapisanej edycji operatora w juz
    wyswietlonej karcie. Kazda odpowiedz osadza nowy `after_item_id` w
    `hx-get` kolejnego pollu (`partials/batch_progress.html`), wiec
    nastepne zapytanie poprosi tylko o pozycje nowsze niz ta juz pokazana.

    Ekstrakcja nadal w toku -> pasek postepu (poling siebie). Zakonczona
    powodzeniem -> pusty #batch-progress bez `hx-trigger` (poling ustaje,
    karty zostaja). Zakonczona bledem -> przycisk "Wznow rozpoznawanie"
    (kontynuuje od miejsca przerwania) z komunikatem bledu.
    """
    new_items = await intake.list_items_after(session, batch_id, after_item_id)
    context: dict[str, object] = {
        "batch_id": batch_id,
        "new_items": new_items,
        "platforms": await intake.list_platforms(session),
        "last_item_id": new_items[-1].id if new_items else after_item_id,
    }

    if batch_id in _running:
        done, total = await intake.extraction_progress(session, batch_id)
        context.update(_progress_context(batch_id, done, total))
        return templates.TemplateResponse(
            request=request,
            name="partials/batch_extract_poll_running.html",
            context=context,
        )

    if await _batch_status(session, batch_id) in ("extracting", "failed"):
        # patrz analogiczny komentarz w batch_detail o statusie 'extracting'
        # bez batch_id w `_running` (proces zabity w trakcie).
        context["extraction_error"] = (
            "Rozpoznawanie zostalo przerwane. Wznowienie kontynuuje od "
            "miejsca przerwania."
        )
    return templates.TemplateResponse(
        request=request, name="partials/batch_extract_poll_done.html", context=context
    )


@router.post("/batches/{batch_id}/publish-all", response_class=HTMLResponse)
async def start_publish_batch(
    request: Request,
    batch_id: int,
    background_tasks: BackgroundTasks,
    session: SessionDep,
) -> HTMLResponse:
    """Wystawia masowa publikacje partii w tle i zwraca fragment postepu.

    Guard idempotencji analogiczny do `start_extraction`: `hx-disabled-elt`
    na przycisku juz blokuje powtorne kliknieicie w przegladarce, ale ten
    guard chroni tez przed wyscigiem/podwojnym zdarzeniem HTMX - dopoki
    poprzedni przebieg partii nie skonczy, nie wystawia drugiego.
    """
    if batch_id not in _running_publish:
        _running_publish.add(batch_id)
        _publish_results.pop(batch_id, None)
        background_tasks.add_task(_run_publish_batch, batch_id)
    done, total = await intake.publish_progress(session, batch_id)
    return templates.TemplateResponse(
        request=request,
        name="partials/batch_publish_progress.html",
        context=_progress_context(batch_id, done, total),
    )


@router.get("/batches/{batch_id}/publish-progress", response_class=HTMLResponse)
async def batch_publish_progress(
    request: Request, batch_id: int, session: SessionDep
) -> HTMLResponse:
    """Fragment polingowany przez HTMX co 2s, dopoki masowa publikacja biegnie.

    Nadal w toku -> pasek postepu z tym samym `hx-get` (poling siebie).
    Zakonczona -> podsumowanie przebiegu (`_publish_results`, zdjete z
    rejestru - jednorazowe) razem z odswiezonymi kartami pozycji (OOB
    swap `#batch-items`, bo statusy pozycji sie zmienily) i przyciskiem
    "Publikuj wszystkie" ponownie, jesli zostaly jeszcze pozycje
    pending/approved (czesciowe niepowodzenie albo przerwanie circuit
    breakerem). Bez `hx-trigger` w zadnej z tych galezi -> poling
    zatrzymuje sie sam.
    """
    if batch_id in _running_publish:
        done, total = await intake.publish_progress(session, batch_id)
        return templates.TemplateResponse(
            request=request,
            name="partials/batch_publish_progress.html",
            context=_progress_context(batch_id, done, total),
        )
    result = _publish_results.pop(batch_id, None)
    items = await intake.list_items(session, batch_id)
    publishable_count = sum(
        1 for item in items if item.status in ("pending", "approved")
    )
    return templates.TemplateResponse(
        request=request,
        name="partials/batch_publish_result.html",
        context={
            "batch_id": batch_id,
            "result": result,
            "items": items,
            "platforms": await intake.list_platforms(session),
            "publishable_count": publishable_count,
            "oob": True,
        },
    )


@router.post("/listings/sync-pending", response_class=HTMLResponse)
async def sync_pending_listings(request: Request, session: SessionDep) -> HTMLResponse:
    """Odswieza statusy ofert czekajacych na aktywacje w OLX (przycisk w naglowku).

    Woła ta sama funkcje serwisowa co `POST /api/listings/sync-pending`.
    Blad OLX (np. brak autoryzacji) nie jest wyjatkiem HTTP - fragment
    pokazuje komunikat, status pozostaje 200, zeby HTMX podmienil DOM.
    """
    try:
        result = await intake.sync_pending_listings(session)
    except (intake.IntakeError, olx.OlxError) as exc:
        return templates.TemplateResponse(
            request=request,
            name="partials/sync_result.html",
            context={"error": str(exc)},
        )
    return templates.TemplateResponse(
        request=request,
        name="partials/sync_result.html",
        context={"result": result},
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
    """Zatwierdza i publikuje pozycje poczekalni na OLX. Operacja nieodwracalna."""
    try:
        item = await intake.approve_and_publish(session, item_id)
    except (intake.IntakeError, olx.OlxError) as exc:
        return await _card(
            request, session, await intake.get_item(session, item_id), str(exc)
        )
    return await _card(request, session, item)
