"""
Read-only client for AWB Arona Warehouse (scripts.arona.ro) — product categories.

The warehouse files every SKU under one category (HA, Grandia, Lavete, Genți, …).
`/api/shelf-list?with_items=1` returns every shelf with its products and their
category in one light call (~0.5 s). Don't use `/api/product-browse` or the
inventory calculator for this: they compute sales per product, and a few of those
calls stalled the warehouse for minutes (24.09.2026) — people scan through it.
"""

import logging
from typing import Dict

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class WarehouseUnavailable(Exception):
    """The warehouse is not configured or did not answer."""


async def fetch_categories() -> Dict[str, str]:
    """SKU (lower-case) → warehouse category, for every product on a shelf."""
    if not (settings.warehouse_api_user and settings.warehouse_api_password):
        raise WarehouseUnavailable(
            "WAREHOUSE_API_USER / WAREHOUSE_API_PASSWORD nu sunt setate în .env"
        )
    try:
        async with httpx.AsyncClient(
            base_url=settings.warehouse_api_url, timeout=30.0
        ) as client:
            resp = await client.post(
                "/api/auth/login",
                json={
                    "username": settings.warehouse_api_user,
                    "password": settings.warehouse_api_password,
                },
            )
            token = resp.json().get("access_token") if resp.status_code == 200 else None
            if not token:
                raise WarehouseUnavailable(f"login warehouse eșuat ({resp.status_code})")
            resp = await client.get(
                "/api/shelf-list",
                params={"with_items": 1},
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            shelves = resp.json().get("shelves") or []
    except (httpx.HTTPError, ValueError) as e:
        raise WarehouseUnavailable(f"warehouse nu răspunde: {e}") from e
    by_sku: Dict[str, str] = {}
    for shelf in shelves:
        for product in shelf.get("products") or []:
            sku = (product.get("sku") or "").strip().lower()
            if sku and product.get("category"):
                by_sku.setdefault(sku, product["category"])
    logger.info("warehouse categories: %d SKUs from %d shelves", len(by_sku), len(shelves))
    return by_sku
