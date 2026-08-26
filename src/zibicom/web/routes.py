"""Router zwracajacy HTML dla interfejsu pracownika."""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from zibicom import intake
from zibicom.db import get_session

router = APIRouter(prefix="/ui", tags=["ui"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@router.get("/batches/{batch_id}", response_class=HTMLResponse)
async def batch_detail(
    request: Request,
    batch_id: int,
    session: SessionDep,
) -> HTMLResponse:
    """Renderuje pozycje poczekalni dla wskazanej partii.

    Args:
        request: Zadanie HTTP wymagane przez Jinja2Templates.
        batch_id: Identyfikator partii przyjecia.
        session: Sesja bazodanowa wstrzykiwana przez FastAPI.

    Returns:
        Strona HTML z lista pozycji partii.
    """
    items = await intake.list_items(session, batch_id)
    return templates.TemplateResponse(
        request=request,
        name="batch_detail.html",
        context={"batch_id": batch_id, "items": items},
    )
