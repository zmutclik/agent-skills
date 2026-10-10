from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.items.models import Item
from app.items.schemas import ItemCreate

PAGE_SIZE = 20


async def list_page(
    db: AsyncSession, *, owner_id: int | None, before_id: int | None, limit: int = PAGE_SIZE
) -> tuple[list[Item], int | None]:
    """Keyset pagination, terbaru dulu. owner_id=None berarti semua item (admin).
    Ambil limit+1 untuk tahu apakah masih ada halaman berikutnya."""
    stmt = select(Item).order_by(Item.id.desc()).limit(limit + 1)
    if owner_id is not None:
        # index owner_id cukup: secondary index InnoDB menyimpan PK, jadi (owner_id, id) terurut
        stmt = stmt.where(Item.owner_id == owner_id)
    if before_id is not None:
        stmt = stmt.where(Item.id < before_id)
    rows = list(await db.scalars(stmt))
    has_more = len(rows) > limit
    items = rows[:limit]
    return items, (items[-1].id if has_more else None)


async def create_item(db: AsyncSession, data: ItemCreate, owner_id: int) -> Item:
    async with db.begin():
        item = Item(**data.model_dump(), owner_id=owner_id)
        db.add(item)
        await db.flush()
        # Kolom server_default (created_at) expired setelah INSERT; dibaca di template setelah commit = lazy load
        # di async → MissingGreenlet. Muat ulang di dalam transaksi.
        await db.refresh(item)
    return item


async def delete_item(db: AsyncSession, item_id: int) -> bool:
    async with db.begin():
        result = await db.execute(delete(Item).where(Item.id == item_id))
    return result.rowcount > 0
