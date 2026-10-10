import json
from datetime import datetime

from sqlalchemy import func, select

from app.core.database import async_session_factory
from app.items import service as item_service
from app.items.models import Item
from app.items.schemas import ItemCreate
from tests.conftest import htmx_headers, login, make_user


async def count(model) -> int:
    async with async_session_factory() as db:
        return await db.scalar(select(func.count()).select_from(model))


async def test_requires_login_redirects_browser_and_htmx(client):
    resp = await client.get("/items")
    assert resp.status_code == 303 and resp.headers["location"] == "/login"

    resp = await client.get("/items", headers=htmx_headers())
    assert resp.status_code == 401 and resp.headers["HX-Redirect"] == "/login"


async def test_full_page_vs_fragment(client):
    await make_user()
    await login(client)

    full = await client.get("/items")
    assert full.status_code == 200 and "<html" in full.text
    assert full.headers["Vary"] == "HX-Request"
    assert "/static/js/alpine/items.js?v=" in full.text                    # dari block scripts halaman, ber-versi
    assert "/static/js/alpine/items.js" not in (await client.get("/login")).text

    fragment = await client.get("/items", headers=htmx_headers())
    assert "<html" not in fragment.text

    restore = await client.get("/items", headers=htmx_headers(**{"HX-History-Restore-Request": "true"}))
    assert "<html" in restore.text


async def test_post_without_csrf_rejected(client):
    await make_user()
    await login(client)
    resp = await client.post("/items", data={"name": "x"}, headers=htmx_headers())
    assert resp.status_code == 403
    assert resp.headers["HX-Retarget"] == "#toast"
    assert await count(Item) == 0


async def test_validation_error_retargets_form(client):
    await make_user()
    csrf = await login(client)
    resp = await client.post("/items", data={"name": "   "}, headers=htmx_headers(csrf))
    assert resp.status_code == 422
    assert resp.headers["HX-Retarget"] == "#item-form"
    assert 'id="item-form"' in resp.text
    assert await count(Item) == 0


async def test_create_item_returns_row_and_trigger(client):
    await make_user()
    csrf = await login(client)
    resp = await client.post("/items", data={"name": "Kursi"}, headers=htmx_headers(csrf))

    assert resp.status_code == 200
    trigger = json.loads(resp.headers["HX-Trigger"])
    assert "item-saved" in trigger
    assert "Kursi" in resp.text

    item_id = trigger["item-saved"]["id"]
    assert f'id="item-{item_id}"' in resp.text
    # created_at (server_default) ikut dirender: service me-refresh di dalam transaksi, bukan lazy load async
    assert resp.text.count("<td>") == 3 and datetime.now().strftime("%Y") in resp.text
    assert await count(Item) == 1


async def test_keyset_pagination(client):
    user = await make_user()
    async with async_session_factory() as db:
        for i in range(25):
            await item_service.create_item(db, ItemCreate(name=f"item-{i:02d}"), owner_id=user.id)
    await login(client)

    first = await client.get("/items", headers=htmx_headers())
    assert first.text.count('id="item-') == 20 and "item-24" in first.text and 'id="load-more"' in first.text

    cursor = first.text.split("/items?before=")[1].split('"')[0]
    second = await client.get(f"/items?before={cursor}", headers=htmx_headers())
    assert second.text.count('id="item-') == 5 and "item-00" in second.text
    assert 'id="load-more"' not in second.text


async def test_delete_requires_admin_role(client):
    owner = await make_user("bob", role="user")
    async with async_session_factory() as db:
        item = await item_service.create_item(db, ItemCreate(name="x"), owner_id=owner.id)

    csrf = await login(client, "bob")
    resp = await client.delete(f"/items/{item.id}", headers=htmx_headers(csrf))
    assert resp.status_code == 403

    await make_user("root", role="admin")
    client.cookies.clear()
    csrf = await login(client, "root")
    resp = await client.delete(f"/items/{item.id}", headers=htmx_headers(csrf))
    assert resp.status_code == 200 and resp.text == ""
    assert await count(Item) == 0


async def test_user_sees_only_own_items_admin_sees_all(client):
    alice = await make_user("alice")
    bob = await make_user("bob")
    await make_user("root", role="admin")
    async with async_session_factory() as db:
        await item_service.create_item(db, ItemCreate(name="punya-alice"), owner_id=alice.id)
    async with async_session_factory() as db:
        await item_service.create_item(db, ItemCreate(name="punya-bob"), owner_id=bob.id)

    await login(client, "alice")
    page = (await client.get("/items", headers=htmx_headers())).text
    assert "punya-alice" in page and "punya-bob" not in page

    client.cookies.clear()
    await login(client, "root")
    page = (await client.get("/items", headers=htmx_headers())).text
    assert "punya-alice" in page and "punya-bob" in page
