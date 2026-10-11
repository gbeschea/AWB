"""
Read-only client for the stock-sync v2 API (master stock + per-store allocation).

stock-sync is the source of truth for stock since the July 2026 cutover —
``products.stock_available`` (fed by the old InventorySync) is frozen since
2026-07-22 and must not be used for coverage numbers.

Endpoints used (all GET, token scopes products:read + stores:read):
    /v1/stores                      → store id ↔ shopDomain
    /v1/listings                    → (store, SKU) → barcode; MATCHED ones map sales,
                                      the rest say why a product isn't live anywhere
    /v1/master-products             → every master product (name, image)
    /v1/stock?barcodes=…            → totalUnits + per-store currentUnits (max 200/call)
    /v1/stock-ledger?occurredFrom=… → every stock movement (to date when goods arrived)
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

STOCK_BATCH_SIZE = 200
PAGE_SIZE = 200
MAX_PARALLEL_STOCK_CALLS = 4


class StockSyncUnavailable(Exception):
    """stock-sync is not configured or did not answer."""


def _headers() -> Dict[str, str]:
    token = settings.stock_sync_api_token
    if not token:
        raise StockSyncUnavailable(
            "STOCK_SYNC_API_TOKEN nu e setat în .env — raportul de stoc nu poate citi masterul."
        )
    return {"Authorization": f"Bearer {token}"}


async def _get(client: httpx.AsyncClient, path: str, params=None) -> dict:
    try:
        resp = await client.get(path, params=params, headers=_headers())
    except httpx.HTTPError as e:
        raise StockSyncUnavailable(f"stock-sync nu răspunde ({path}): {e}") from e
    if resp.status_code != 200:
        raise StockSyncUnavailable(
            f"stock-sync a răspuns {resp.status_code} la {path}: {resp.text[:200]}"
        )
    return resp.json()


async def _paginate(client: httpx.AsyncClient, path: str, key: str, params: dict):
    items: List[dict] = []
    cursor = None
    while True:
        page = await _get(
            client, path, {**params, **({"cursor": cursor} if cursor else {})}
        )
        items.extend(page.get(key) or [])
        cursor = page.get("nextCursor")
        if not cursor:
            return items


async def fetch_snapshot() -> dict:
    """Stores, matched listings and stock for every master product, in one pass.

    Stock is fetched for ALL master products, not only listed ones: ~600 masters
    have no listing anywhere yet hold most of the unallocated units.
    """
    async with httpx.AsyncClient(
        base_url=settings.stock_sync_api_url, timeout=60.0
    ) as client:
        stores = (await _get(client, "/v1/stores")).get("stores") or []
        all_listings = await _paginate(
            client, "/v1/listings", "listings", {"limit": PAGE_SIZE}
        )
        listings = [x for x in all_listings if x.get("matchStatus") == "MATCHED"]
        other_listings = [
            x for x in all_listings if x.get("matchStatus") != "MATCHED"
        ]
        masters = await _paginate(
            client, "/v1/master-products", "masterProducts", {"limit": PAGE_SIZE}
        )

        barcodes = sorted(
            {m["barcodeNormalized"] for m in masters if m.get("barcodeNormalized")}
            | {x["barcodeNormalized"] for x in listings if x.get("barcodeNormalized")}
        )
        sem = asyncio.Semaphore(MAX_PARALLEL_STOCK_CALLS)

        async def _stock_batch(batch: List[str]) -> List[dict]:
            async with sem:
                data = await _get(client, "/v1/stock", {"barcodes": ",".join(batch)})
                return data.get("products") or []

        batches = [
            barcodes[i : i + STOCK_BATCH_SIZE]
            for i in range(0, len(barcodes), STOCK_BATCH_SIZE)
        ]
        results = await asyncio.gather(*(_stock_batch(b) for b in batches))

    stock = {p["barcode"]: p for batch in results for p in batch if p.get("barcode")}
    logger.info(
        "stock-sync snapshot: %d stores, %d listings, %d masters, %d with stock",
        len(stores),
        len(listings),
        len(masters),
        len(stock),
    )
    return {
        "stores": stores,
        "listings": listings,
        "other_listings": other_listings,
        "masters": masters,
        "stock": stock,
    }


async def fetch_ledger(occurred_from: str, chunks: int = 8) -> List[dict]:
    """Every stock movement since `occurred_from` (ISO date).

    ~80k entries for three months at 200 per page; the range is split into `chunks`
    time windows fetched in parallel (~40 s sequential → a few seconds).
    """
    start = datetime.fromisoformat(occurred_from).replace(tzinfo=timezone.utc)
    end = datetime.now(timezone.utc) + timedelta(minutes=5)
    step = (end - start) / chunks
    bounds = [(start + step * i, start + step * (i + 1)) for i in range(chunks)]

    async with httpx.AsyncClient(
        base_url=settings.stock_sync_api_url, timeout=60.0
    ) as client:

        async def _window(lo: datetime, hi: datetime) -> List[dict]:
            return await _paginate(
                client,
                "/v1/stock-ledger",
                "entries",
                {
                    "limit": PAGE_SIZE,
                    "occurredFrom": lo.isoformat().replace("+00:00", "Z"),
                    "occurredTo": hi.isoformat().replace("+00:00", "Z"),
                },
            )

        pages = await asyncio.gather(*(_window(lo, hi) for lo, hi in bounds))

    seen = set()
    out = []
    for e in (e for page in pages for e in page):
        # Window edges are inclusive on both sides in some APIs — drop duplicates.
        if not e.get("masterProductId") or e.get("id") in seen:
            continue
        seen.add(e.get("id"))
        out.append(
            {
                "id": e.get("id"),
                "master": e["masterProductId"],
                "at": e["occurredAt"],
                "created": e.get("createdAt") or "",
                "before": e.get("balanceBefore") or 0,
                "after": e.get("balanceAfter") or 0,
                "reason": e.get("reason") or "",
                "note": e.get("note") or "",
            }
        )
    return out


async def fetch_master_ledger(master_id: str, limit: int = 60) -> List[dict]:
    """Istoricul de stoc al UNUI master: fiecare mișcare, cu motiv, notă și cine a făcut-o.

    E singurul loc unde se vede DE CE s-a schimbat stocul — recepție, inventar, corecție
    manuală, reconciliere din magazin. Exact lanțul care a scos la iveală masterii duplicat
    cu stoc fantomă (600 de draperii scrise pe un master arhivat).
    """
    async with httpx.AsyncClient(
        base_url=settings.stock_sync_api_url, timeout=30.0
    ) as client:
        data = await _get(client, f"/v1/master-products/{master_id}/ledger", {"limit": limit})
        return data.get("entries") or []
