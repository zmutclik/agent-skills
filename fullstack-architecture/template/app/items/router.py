import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import ValidationError

from app.auth.dependencies import CurrentUserDep, require_csrf, require_role
from app.core.database import DbSession
from app.core.templating import render, templates
from app.items import service as item_service
from app.items.schemas import ItemCreate

# require_csrf juga menjalankan get_current_user (di-cache per request oleh FastAPI)
router = APIRouter(prefix="/items", tags=["items"], dependencies=[Depends(require_csrf)])


@router.get("")
async def list_items(request: Request, user: CurrentUserDep, db: DbSession, before: int | None = None):
    # Otorisasi data: user hanya melihat miliknya; admin melihat semua
    owner_id = None if user.role == "admin" else user.id
    items, next_cursor = await item_service.list_page(db, owner_id=owner_id, before_id=before)
    return render(
        request, "items/list.html", "items/_rows.html",
        {"items": items, "next_cursor": next_cursor, "user": user},
    )


@router.post("")
async def create_item(request: Request, user: CurrentUserDep, db: DbSession):
    # 1. HTMX mengirim form-urlencoded, bukan JSON → validasi manual agar error dirender sebagai HTML
    form = await request.form()
    try:
        data = ItemCreate.model_validate({"name": form.get("name", "")})
    except ValidationError as err:
        # hx-target form = daftar baris; error harus kembali ke form, bukan masuk ke tabel
        return templates.TemplateResponse(
            request=request, name="items/_form.html",
            context={"form": form, "errors": err.errors(include_context=False)}, status_code=422,
            headers={"HX-Retarget": "#item-form", "HX-Reswap": "outerHTML"},
        )

    # 2. Service pemilik transaksi
    item = await item_service.create_item(db, data, owner_id=user.id)

    # 3. Beri tahu Alpine lewat header (event kebab-case), bukan <script> inline di fragment
    return templates.TemplateResponse(
        request=request, name="items/_row.html", context={"item": item, "user": user},
        headers={"HX-Trigger": json.dumps({"item-saved": {"id": item.id}})},
    )


@router.delete("/{item_id}", dependencies=[Depends(require_role("admin"))])
async def delete_item(item_id: int, db: DbSession):
    if not await item_service.delete_item(db, item_id):
        raise HTTPException(status_code=404)
    return Response(status_code=200)   # body kosong + hx-swap="outerHTML" = baris hilang
