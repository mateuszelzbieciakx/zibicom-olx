"""Router zwracający HTML dla interfejsu pracownika.

Równoległy do JSON API w `zibicom.routers` - te same funkcje serwisowe,
inna reprezentacja. Logika biznesowa nie może istnieć w dwóch kopiach.
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

# Partie, których ekstrakcja aktualnie biegnie w tle (guard idempotencji dla
# POST /batches/{id}/extract - żyje tylko w pamięci procesu, patrz komentarz
# przy _run_extraction o utracie postępu przy restarcie).
_running: set[int] = set()

# Analogiczny guard idempotencji dla POST /batches/{id}/publish-all - drugi
# klik/zdarzenie HTMX, dopóki poprzedni przebieg nie skończy, nie wystawia
# drugiego zadania w tle (patrz uzasadnienie sekwencyjności w
# intake.publish_batch: równoległe zadania OLX mogą trwale unieważnić
# autoryzację przez wyścig o rotację refresh tokenu).
_running_publish: set[int] = set()

# Wynik ostatniego zakończonego przebiegu `publish_batch`, do jednorazowego
# pokazania w podsumowaniu po tym, jak batch_id zniknie z _running_publish -
# `batch_publish_progress` go odczytuje i usuwa (patrz tamten docstring).
# Żyje tylko w pamięci procesu, tak samo jak _running/_running_publish.
_publish_results: dict[int, intake.BulkPublishResult] = {}


async def _card(
    request: Request,
    session: AsyncSession,
    item: intake.IntakeItemView,
    error: str | None = None,
) -> HTMLResponse:
    """Renderuje fragment karty pozycji.

    Args:
        request: Żądanie HTTP wymagane przez Jinja2Templates.
        session: Sesja bazodanowa (do słownika platform).
        item: Widok pozycji do wyświetlenia.
        error: Komunikat błędu domenowego do pokazania w karcie.

    Returns:
        Fragment HTML karty, zawsze ze statusem 200 - HTMX podmienia DOM
        wyłącznie przy 2xx, więc błąd walidacji musi przyjść jako poprawna
        odpowiedź z treścią błędu, nie jako HTTPException.
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
    """Uruchamia `extract_batch` w tle, na własnej sesji DB.

    Własna sesja, bo ta z zadania HTTP jest zamykana, gdy tylko odpowiedź
    zostanie wysłana - `extract_batch` musi działać długo po tym momencie.

    Restart procesu w trakcie ekstrakcji NIE gubi już zrobionej pracy -
    `intake.extract_batch` jest wznawialna (commituje każde rozpoznane
    zdjęcie i każdy domknięty egzemplarz na bieżąco) - traci się tylko
    fakt bycia "w toku": `_running` żyje wyłącznie w pamięci procesu, więc
    po restarcie batch_detail pokaże przycisk "Wznów rozpoznawanie"
    zamiast paska postępu, dopóki operator go nie kliknie. Docelowo:
    prawdziwa kolejka zadań (np. arq/celery) zamiast BackgroundTasks, żeby
    wznowienie było automatyczne.
    """
    try:
        async with get_sessionmaker()() as session:
            await intake.extract_batch(session, batch_id)
    except Exception:
        logger.exception("Ekstrakcja partii %s w tle nie powiodła się.", batch_id)
    finally:
        _running.discard(batch_id)


async def _run_publish_batch(batch_id: int) -> None:
    """Uruchamia `publish_batch` w tle, na własnej sesji DB.

    Własna sesja z tego samego powodu co `_run_extraction`: zadanie HTTP,
    które wystawiło to zadanie w tle, już zakończyło odpowiedź, a masowa
    publikacja partii może trwać minuty (150 pozycji x ~1,3s, patrz
    `intake.publish_batch`). Wynik trafia do `_publish_results` PRZED
    usunięciem batch_id z `_running_publish`, żeby poling
    `/publish-progress` nigdy nie zobaczył "już nie biegnie" bez gotowego
    wyniku do pokazania.
    """
    try:
        async with get_sessionmaker()() as session:
            _publish_results[batch_id] = await intake.publish_batch(session, batch_id)
    except Exception:
        logger.exception(
            "Masowa publikacja partii %s w tle nie powiodła się.", batch_id
        )
    finally:
        _running_publish.discard(batch_id)


@router.get("/batches", response_class=HTMLResponse)
async def batches(request: Request, session: SessionDep) -> HTMLResponse:
    """Renderuje listę wszystkich partii, od najnowszej."""
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
        list[UploadFile], File(description="Zdjęcia w kolejności wgrania.")
    ],
) -> HTMLResponse:
    """Tworzy nową partię z plików wgranych przez formularz w przeglądarce.

    Zwykły POST/Redirect/Get (bez HTMX): po sukcesie 303 See Other na
    `/ui/batches/{batch_id}`, żeby odświeżenie strony nie powtórzyło uploadu
    (307 by to zrobiło). Błąd domenowy re-renderuje listę partii z
    komunikatem, statusem 200 - nie ma dokąd przekierować, bo partia nie
    powstała.
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
        # do tego id są już wyrenderowane na stronie (batch_items.html
        # powyżej) - bez tego pierwszy poling dostałby je jako "nowe" i
        # dopiął drugi raz (patrz batch_extraction_progress/after_item_id).
        context["last_item_id"] = items[-1].id if items else 0
    elif publishing:
        done, total = await intake.publish_progress(session, batch_id)
        context.update(_progress_context(batch_id, done, total))
    elif batch_status in ("extracting", "failed"):
        # 'extracting' bez batch_id w `_running` = proces został zabity w
        # trakcie (restart, OOM) - żaden Python-owy except nie zdążył
        # ustawić 'failed'. Wznowienie jest tak samo bezpieczne jak po
        # czystym błędzie (`intake.extract_batch` jest wznawialna).
        context["extraction_error"] = (
            "Rozpoznawanie zostało przerwane. Wznowienie kontynuuje od "
            "miejsca przerwania (już rozpoznane zdjęcia i zapisane "
            "egzemplarze nie zostaną utworzone drugi raz)."
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
    """Wystawia (lub wznawia) rozpoznanie partii w tle i zwraca pasek postępu.

    Guard idempotencji: TYLKO jeśli ekstrakcja tej partii już biegnie w
    tym procesie (`_running`) NIE wystawia drugiego zadania - to jedyny
    warunek, który ma teraz znaczenie. `intake.extract_batch` jest
    wznawialna: ponowne wywołanie na już ukończonej albo częściowo
    przetworzonej partii jest bezpieczne samo w sobie (pomija zdjęcia z
    wypełnionym ai_raw i egzemplarze już zapisane), więc w odróżnieniu od
    poprzedniej (atomowej) wersji nie trzeba już sprawdzać, czy partia ma
    już pozycje.
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

    Dopina WYŁĄCZNIE nowo domknięte egzemplarze (id > after_item_id) do
    #batch-items przez OOB append (`hx-swap-oob="beforeend"` -
    `partials/batch_items_append.html`) - NIGDY nie przerenderowywuje
    całej listy, żeby nie skasować niezapisanej edycji operatora w już
    wyświetlonej karcie. Każda odpowiedź osadza nowy `after_item_id` w
    `hx-get` kolejnego pollu (`partials/batch_progress.html`), więc
    następne zapytanie poprosi tylko o pozycje nowsze niż ta już pokazana.

    Ekstrakcja nadal w toku -> pasek postępu (poling siebie). Zakończona
    powodzeniem -> pusty #batch-progress bez `hx-trigger` (poling ustaje,
    karty zostają). Zakończona błędem -> przycisk "Wznów rozpoznawanie"
    (kontynuuje od miejsca przerwania) z komunikatem błędu.
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
            "Rozpoznawanie zostało przerwane. Wznowienie kontynuuje od "
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
    """Wystawia masową publikację partii w tle i zwraca fragment postępu.

    Guard idempotencji analogiczny do `start_extraction`: `hx-disabled-elt`
    na przycisku już blokuje powtórne kliknięcie w przeglądarce, ale ten
    guard chroni też przed wyścigiem/podwójnym zdarzeniem HTMX - dopóki
    poprzedni przebieg partii nie skończy, nie wystawia drugiego.
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
    """Fragment polingowany przez HTMX co 2s, dopóki masowa publikacja biegnie.

    Nadal w toku -> pasek postępu z tym samym `hx-get` (poling siebie).
    Zakończona -> podsumowanie przebiegu (`_publish_results`, zdjęte z
    rejestru - jednorazowe) razem z odświeżonymi kartami pozycji (OOB
    swap `#batch-items`, bo statusy pozycji się zmieniły) i przyciskiem
    "Publikuj wszystkie" ponownie, jeśli zostały jeszcze pozycje
    pending/approved (częściowe niepowodzenie albo przerwanie circuit
    breakerem). Bez `hx-trigger` w żadnej z tych gałęzi -> poling
    zatrzymuje się sam.
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
    """Odświeża statusy ofert czekających na aktywację w OLX (przycisk w nagłówku).

    Woła tą samą funkcję serwisową co `POST /api/listings/sync-pending`.
    Błąd OLX (np. brak autoryzacji) nie jest wyjątkiem HTTP - fragment
    pokazuje komunikat, status pozostaje 200, żeby HTMX podmienił DOM.
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
    """Zapisuje ręczną korektę pól pozycji.

    Formularz HTML przysyła wszystkie pola jako tekst (puste = pusty string),
    więc konwersja na typy domenowe odbywa się tutaj - `IntakeItemUpdate`
    dostaje już wartości właściwego typu.
    """
    try:
        price = Decimal(price_pln) if price_pln.strip() else None
    except InvalidOperation:
        current = await intake.get_item(session, item_id)
        return await _card(request, session, current, "Cena musi być liczbą.")

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
    """Zatwierdza pozycję poczekalni."""
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
    """Odrzuca pozycję poczekalni."""
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
    """Zatwierdza i publikuje pozycję poczekalni na OLX. Operacja nieodwracalna."""
    try:
        item = await intake.approve_and_publish(session, item_id)
    except (intake.IntakeError, olx.OlxError) as exc:
        return await _card(
            request, session, await intake.get_item(session, item_id), str(exc)
        )
    return await _card(request, session, item)
