"""
Stoc & Viteză — per-store stock coverage report.

Answers "what isn't selling": for every product — per store, or across all stores —
its stock (stock-sync master), units sold in the last 30 and 90 days (AWB orders;
cancelled, refused and returned parcels are not sales), days since the last sale, how
long the stock lasts and what it's worth.
"""

import asyncio
import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

BUCHAREST_TZ = ZoneInfo("Europe/Bucharest")

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.sku_risk.computations import compute_final_outcome
from app.api.stock_coverage.ads_tiers import annotate as annotate_ads
from app.api.stock_coverage.categories import categorize
from app.api.stock_coverage.computations import (
    ALL_STORES_NAME,
    ALL_STORES_UID,
    UNALLOCATED_STORE_NAME,
    UNALLOCATED_STORE_UID,
    DEAD_DAYS,
    awb_store_domain,
    LEDGER_CUTOVER,
    build_rows,
    physically_counted,
    pick_shopify_costs,
    stock_arrivals,
)
from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.order_filters import load_exclusion_rules
from app.models.product import Product
from app.models.profitability_config import ProfitabilityConfig
from app.models.sku_cost import SkuCost
from app.services.stock_sync_client import (
    StockSyncUnavailable,
    fetch_ledger,
    fetch_master_ledger,
    fetch_snapshot,
)
from app.services import public_stock
from app.services import warehouse_service as ws
from app.services.warehouse_client import WarehouseUnavailable, fetch_categories

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analytics", tags=["analytics"])

# One report for "what doesn't sell right now": sales over the last 30 days next to the
# last 90 (dead stock = nothing in 90), the pace taken from 30 days, else from 90.
CURRENT_DAYS = 30
ALLOWED_PERIODS = (CURRENT_DAYS, DEAD_DAYS)
# Parcels still on their way are expected to come back at the SKU's own refusal rate
# (90 days), else the store's, else the overall one — whichever has enough resolved units.
MIN_UNITS_FOR_SKU_REFUSAL_RATE = 30
MIN_UNITS_FOR_STORE_REFUSAL_RATE = 50
COSTS_TTL_SECONDS = 3600
COST_SNAPSHOT = Path(__file__).resolve().parents[3] / "data" / "stock-coverage-costs.json"
NO_CATEGORY = "Fără categorie"
# A product with no category anywhere that no store sells and that holds no stock: an
# old catalog row, kept for its history.
DELISTED_CATEGORY = "Scos din vânzare"
# The warehouse's own fallback word: means "it couldn't tell", so our rules get a go.
WAREHOUSE_UNKNOWN = "Diverse"
CATEGORIES_TTL_SECONDS = 3600
# Fallback when this server has no warehouse login: a {generated_at, by_sku} snapshot
# of the shelf list, dropped on the server (not in git).
CATEGORY_SNAPSHOT = (
    Path(__file__).resolve().parents[3] / "data" / "stock-coverage-categories.json"
)
# Goods that came back or never left are not sold: cancelled, refused, returned
# (Gigi's rule; "Viteză Vânzări" counts refusals, so its units can be higher).
NOT_SOLD_OUTCOMES = {"CANCELLED", "REFUSED", "BACK_TO_SENDER"}
# The report is kept built in the background, so nobody waits for a cold build.
WARM_INTERVAL_SECONDS = 300
# Stock only moves at the stock-sync runs (02:00 + manual), so a longer TTL is safe.
# Own cache, not app.core.analytics_cache: that one is wiped by every order sync
# (~10 min), which made store-filter changes and row details rebuild everything.
REPORT_TTL_SECONDS = 600
SNAPSHOT_TTL_SECONDS = 600
SALES_TTL_SECONDS = 600
LAST_SALES_TTL_SECONDS = 1800
LEDGER_TTL_SECONDS = 1800
LAST_SALE_LOOKBACK_DAYS = 365
# Ad spend per product for the ads tiers: the same 30 days as the sales next to it,
# whole days only (today is still filling in).
AD_SPEND_DAYS = CURRENT_DAYS
AD_SPEND_TTL_SECONDS = 3600
AD_SPEND_SQL = (
    "SELECT sku, platform, SUM(spend_ron)::float8, MAX(date) "
    "FROM cache.product_ad_spend "
    "WHERE date >= CURRENT_DATE - $1::int AND date < CURRENT_DATE AND spend_ron > 0 "
    "GROUP BY sku, platform"
)

_CACHE: dict = {}
_LOCKS: dict = {}
_REFRESHING: set = set()


async def _refresh(key: str, ttl: int, loader):
    lock = _LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        entry = _CACHE.get(key)
        if entry and entry[0] > time.monotonic():
            return entry[1]
        value = await loader()
        _CACHE[key] = (time.monotonic() + ttl, value)
        return value


async def _refresh_in_background(key: str, ttl: int, loader):
    try:
        await _refresh(key, ttl, loader)
    except Exception:
        logger.exception("stock-coverage: background refresh of %s failed", key)
    finally:
        _REFRESHING.discard(key)


async def _cached(key: str, ttl: int, loader):
    """Per-process cache that never makes a user wait twice: once a value exists, an
    expired one is served immediately while a single background task rebuilds it.
    The lock stops concurrent requests from rebuilding in parallel."""
    entry = _CACHE.get(key)
    if entry:
        if entry[0] <= time.monotonic() and key not in _REFRESHING:
            _REFRESHING.add(key)
            asyncio.create_task(_refresh_in_background(key, ttl, loader))
        return entry[1]
    return await _refresh(key, ttl, loader)


def _period_bounds(days: int):
    """Last `days` calendar days in Bucharest time (today included), as naive UTC."""
    now_buc = datetime.now(ZoneInfo("Europe/Bucharest"))
    start_buc = (now_buc - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    to_utc = lambda d: d.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)  # noqa: E731
    return to_utc(start_buc), to_utc(now_buc)


async def _load_stores(db: AsyncSession, stock_sync_stores: list) -> list:
    ss_names = {
        (s.get("shopDomain") or "").lower(): s.get("name") for s in stock_sync_stores
    }
    # xconnector_domain exists in the prod table but not on the Store model.
    rows = await db.execute(
        text(
            "SELECT uid, name, shopify_domain, frisbo_store_slug, xconnector_domain "
            "FROM stores WHERE is_active"
        )
    )
    stores = []
    for uid, name, shopify_domain, slug, xconnector_domain in rows.all():
        domain = awb_store_domain(uid, xconnector_domain, shopify_domain, slug)
        # A few stores were created without a name — their uid shows instead.
        if not name or name == uid:
            name = ss_names.get(domain) or uid
        stores.append({"uid": uid, "name": name, "domain": domain})
    return stores


async def _load_catalog(db: AsyncSession):
    """Active AWB products per store, plus when each SKU first appeared in AWB — in
    any state: a product drafted or archived since still says how old it is."""
    first_rows = await db.execute(
        select(Product.sku, func.min(Product.frisbo_created_at))
        .where(Product.sku.isnot(None), Product.sku != "")
        .group_by(Product.sku)
    )
    first_seen: dict = {}
    for sku, created in first_rows.all():
        key = sku.strip()
        if created and (key not in first_seen or created < first_seen[key]):
            first_seen[key] = created
    rows = await db.execute(
        select(
            Product.store_uids,
            Product.sku,
            Product.barcode,
            Product.title_1,
        ).where(
            Product.state == "active",
            Product.sku.isnot(None),
            Product.sku != "",
        )
    )
    catalog = []
    for store_uids, sku, barcode, title in rows.all():
        if isinstance(store_uids, str):
            try:
                store_uids = json.loads(store_uids)
            except json.JSONDecodeError:
                store_uids = []
        for uid in store_uids or []:
            catalog.append((uid, sku, barcode, title))
    return catalog, first_seen


async def _load_store_activity(db: AsyncSession) -> dict:
    """store → (first order, last order): a young store can't have dead stock yet, a
    store with no recent order has unreliable sales history."""
    rows = await db.execute(
        text(
            "SELECT store_uid, MIN(frisbo_created_at), MAX(frisbo_created_at) "
            "FROM orders GROUP BY store_uid"
        )
    )
    first, last = {}, {}
    for uid, lo, hi in rows.all():
        if uid and lo:
            first[uid], last[uid] = lo, hi
    return {"first": first, "last": last}


async def _load_test_skus(db: AsyncSession) -> set:
    """SKUs still in their test period: only test-tagged orders over the lookback,
    never a real (non-test, non-cancelled) one."""
    rows = await db.execute(
        text(
            "SELECT e->'inventory_item'->>'sku' AS sku, "
            "BOOL_OR(COALESCE(o.tags::text, '') ILIKE '%\"test\"%') AS has_test, "
            "BOOL_OR(COALESCE(o.tags::text, '') NOT ILIKE '%\"test\"%' "
            "AND COALESCE(o.aggregated_status, '') <> 'cancelled') AS has_real "
            "FROM orders o, jsonb_array_elements(o.line_items::jsonb) e "
            "WHERE o.frisbo_created_at >= :since GROUP BY 1"
        ),
        {"since": datetime.utcnow() - timedelta(days=LAST_SALE_LOOKBACK_DAYS)},
    )
    return {
        sku.strip()
        for sku, has_test, has_real in rows.all()
        if sku and has_test and not has_real
    }


# The ledger since the cutover grows forever; each worker reads it once, then only
# what's new. "Counted" only ever grows; arrivals need the last ~90 days.
_LEDGER: dict = {"through": None, "recent": {}, "counted": set()}
LEDGER_OVERLAP = timedelta(hours=6)


async def _load_ledger_facts() -> dict:
    """From the stock-sync ledger: when goods last arrived, and which products were
    ever physically counted since the cutover."""
    started = datetime.utcnow()
    since = (
        (_LEDGER["through"] - LEDGER_OVERLAP).strftime("%Y-%m-%dT%H:%M:%S")
        if _LEDGER["through"]
        else LEDGER_CUTOVER
    )
    entries = await fetch_ledger(since)
    _LEDGER["counted"] |= physically_counted(entries)
    keep_from = (started - timedelta(days=DEAD_DAYS + 5)).strftime("%Y-%m-%d")
    recent = _LEDGER["recent"]
    for e in entries:
        if e["at"] >= keep_from:
            recent[e.get("id") or (e["master"], e["at"], e["created"])] = e
    for key in [k for k, e in recent.items() if e["at"] < keep_from]:
        del recent[key]
    _LEDGER["through"] = started
    return {
        "arrivals": stock_arrivals(recent.values()),
        "counted": set(_LEDGER["counted"]),
    }


async def _with_session(loader):
    async with AsyncSessionLocal() as db:
        return await loader(db)


async def _load_sales_history(db: AsyncSession) -> dict:
    """One pass over a year of orders, aggregated in Postgres per (store, SKU, status
    fields, Bucharest day) — every period, the first / last sale and the yearly pace
    all come from it, instead of re-reading the orders for each.

    Grouped by the three status fields so the outcome (cancelled / refused / returned /
    delivered / on its way) is decided by compute_final_outcome.
    """
    excluded_tags, _ = await load_exclusion_rules(db)
    today = datetime.now(BUCHAREST_TZ).date()
    params = {"since": datetime.utcnow() - timedelta(days=LAST_SALE_LOOKBACK_DAYS)}
    tag_sql = ""
    for i, tag in enumerate(excluded_tags):
        params[f"tag{i}"] = f'%"{tag}"%'
        tag_sql += f" AND (o.tags IS NULL OR o.tags::text NOT ILIKE :tag{i})"
    local_day = "(o.frisbo_created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Bucharest')::date"
    rows = await db.execute(
        text(
            "SELECT o.store_uid, btrim(e->'inventory_item'->>'sku') AS sku, "
            "o.aggregated_status, o.shipment_status, o.fulfillment_status, "
            f"{local_day} AS day, "
            "SUM(COALESCE(NULLIF(NULLIF(e->>'quantity', ''), '0')::numeric, 1)), "
            "MAX(o.frisbo_created_at), MAX(e->'inventory_item'->>'title_1') "
            "FROM orders o, jsonb_array_elements(o.line_items::jsonb) e "
            "WHERE o.frisbo_created_at >= :since"
            + tag_sql
            + " AND COALESCE(e->'inventory_item'->>'sku', '') <> '' "
            "GROUP BY 1, 2, 3, 4, 5, 6"
        ),
        params,
    )
    return {"today": today, "rows": rows.all()}


def _summarize_sales(history: dict, days: int):
    """Period sales, yearly sales, last-sale dates and sale days, keyed (store, SKU).

    `units` = parcels not cancelled, refused or returned. `net_units` also expects part
    of the parcels still on their way to come back — the store's refusal rate over the
    last 90 days (16-25% of units return; Gigi's review) — and drives the velocity.
    """
    today = history["today"]
    start = today - timedelta(days=days - 1)
    refusal_from = today - timedelta(days=DEAD_DAYS - 1)
    outcome_cache: dict = {}

    def outcome(statuses):
        if statuses not in outcome_cache:
            outcome_cache[statuses] = compute_final_outcome(*statuses)
        return outcome_cache[statuses]

    delivered, returned = defaultdict(float), defaultdict(float)
    for store_uid, sku, workflow, shipment, fulfillment, day, units, _, _ in history["rows"]:
        if day and day >= refusal_from:
            result = outcome((workflow, shipment, fulfillment))
            target = (
                delivered
                if result == "DELIVERED"
                else returned
                if result in ("REFUSED", "BACK_TO_SENDER")
                else None
            )
            if target is not None:
                target[store_uid or ""] += float(units or 0)
                target[(store_uid or "", sku)] += float(units or 0)
    store_keys = [k for k in delivered.keys() | returned.keys() if isinstance(k, str)]
    all_delivered = sum(delivered[k] for k in store_keys)
    all_returned = sum(returned[k] for k in store_keys)
    overall_rate = (
        all_returned / (all_delivered + all_returned)
        if all_delivered + all_returned
        else 0.0
    )

    def refusal_rate(key: tuple) -> float:
        for scope, minimum in (
            (key, MIN_UNITS_FOR_SKU_REFUSAL_RATE),
            (key[0], MIN_UNITS_FOR_STORE_REFUSAL_RATE),
        ):
            resolved = delivered[scope] + returned[scope]
            if resolved >= minimum:
                return returned[scope] / resolved
        return overall_rate

    sales = defaultdict(lambda: {"units": 0.0, "net_units": 0.0, "name": ""})
    year: dict = defaultdict(float)
    last_sales: dict = {}
    sale_days: dict = defaultdict(set)
    for store_uid, sku, workflow, shipment, fulfillment, day, units, last, name in history["rows"]:
        result = outcome((workflow, shipment, fulfillment))
        if result in NOT_SOLD_OUTCOMES:
            continue
        key = (store_uid or "", sku)
        units = float(units or 0)
        year[key] += units
        if day:
            sale_days[key].add(day)
        if last and (key not in last_sales or last > last_sales[key]):
            last_sales[key] = last
        if day and day >= start:
            agg = sales[key]
            agg["units"] += units
            agg["net_units"] += (
                units if result == "DELIVERED" else units * (1 - refusal_rate(key))
            )
            if not agg["name"] and name:
                agg["name"] = name
    return dict(sales), dict(year), last_sales, dict(sale_days)


async def _load_costs(db: AsyncSession) -> dict:
    """SKU → unit cost from sku_costs (with VAT, the same figures as Shopify's
    costPerItem on 614 of 811 products) — only a fallback: the table is maintained by
    hand and stale since 9 June."""
    rows = await db.execute(select(SkuCost.sku, SkuCost.cost))
    return {sku.strip(): float(cost) for sku, cost in rows.all() if sku and cost}


async def _load_vat_rate(db: AsyncSession) -> float:
    config = (
        await db.execute(select(ProfitabilityConfig).limit(1))
    ).scalar_one_or_none()
    return float(config.vat_rate) if config and config.vat_rate else 0.0



SHOPIFY_COST_SQL = (
    'SELECT v.barcode, v."costPerItem"::float, v."updatedAt" '
    "FROM variants v JOIN shopify_stores s ON s.\"brandId\" = v.\"brandId\" "
    'WHERE v."deletedAt" IS NULL AND v."costPerItem" > 0 '
    "AND s.\"nativeCurrency\" = 'RON' AND COALESCE(v.barcode, '') <> ''"
)


def _read_cost_snapshot() -> dict:
    try:
        return json.loads(COST_SNAPSHOT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


async def _load_shopify_costs() -> dict:
    """barcode → Shopify costPerItem (RON stores) from the metrics warehouse, or from
    the snapshot when this server has no metrics DSN."""
    if settings.metrics_database_url:
        try:
            import asyncpg

            conn = await asyncpg.connect(settings.metrics_database_url, timeout=20)
            try:
                rows = await conn.fetch(SHOPIFY_COST_SQL)
            finally:
                await conn.close()
            return {
                "by_barcode": pick_shopify_costs((r[0], r[1], r[2]) for r in rows),
                "as_of": datetime.utcnow().isoformat(),
            }
        except Exception as e:
            logger.warning("stock-coverage: Shopify costs unavailable live: %s", e)
    snapshot = await asyncio.to_thread(_read_cost_snapshot)
    if snapshot.get("by_barcode"):
        return {"by_barcode": snapshot["by_barcode"], "as_of": snapshot.get("generated_at")}
    return {}


async def _load_ad_spend() -> dict:
    """(sku or product group, platform, RON) over the last AD_SPEND_DAYS whole days from
    the metrics warehouse; {} without it (the page then shows no spend, tiers still work)."""
    if not settings.metrics_database_url:
        return {}
    try:
        import asyncpg

        conn = await asyncpg.connect(settings.metrics_database_url, timeout=20)
        try:
            rows = await conn.fetch(AD_SPEND_SQL, AD_SPEND_DAYS)
        finally:
            await conn.close()
    except Exception as e:
        logger.warning("stock-coverage: ad spend unavailable: %s", e)
        return {}
    last = max((r[3] for r in rows if r[3]), default=None)
    return {
        "rows": [(r[0], r[1], r[2]) for r in rows],
        "last_day": last.isoformat() if last else None,
    }


async def _build_report(days: int) -> dict:
    # Own sessions throughout: this also runs as a background refresh after the
    # request that triggered it is gone.
    snapshot, ledger_facts = await asyncio.gather(
        _cached("snapshot", SNAPSHOT_TTL_SECONDS, fetch_snapshot),
        _cached("ledger-facts", LEDGER_TTL_SECONDS, _load_ledger_facts),
    )
    history = await _cached(
        "sales-history", SALES_TTL_SECONDS, lambda: _with_session(_load_sales_history)
    )
    test_skus = await _cached(
        "test-skus", LAST_SALES_TTL_SECONDS, lambda: _with_session(_load_test_skus)
    )
    activity = await _cached(
        "store-activity",
        LAST_SALES_TTL_SECONDS,
        lambda: _with_session(_load_store_activity),
    )
    async with AsyncSessionLocal() as db:
        stores = await _load_stores(db, snapshot["stores"])
        catalog, first_seen = await _load_catalog(db)
        costs = await _load_costs(db)
        vat_rate = await _load_vat_rate(db)
    shopify = await _cached("shopify-costs", COSTS_TTL_SECONDS, _load_shopify_costs)
    dt_from, dt_to = _period_bounds(days)
    sales, year_sales, last_sales, sale_days = await asyncio.to_thread(
        _summarize_sales, history, days
    )
    result = await asyncio.to_thread(
        build_rows,
        stores,
        snapshot,
        catalog,
        sales,
        days,
        last_sales,
        costs,
        first_seen=first_seen,
        store_first_order=activity["first"],
        arrivals=ledger_facts["arrivals"],
        counted=ledger_facts["counted"],
        test_skus=test_skus,
        store_last_order=activity["last"],
        year_sales=year_sales,
        sale_days=sale_days,
        shopify_costs=shopify.get("by_barcode") or {},
        vat_rate=vat_rate,
    )
    excluded = set(result["excluded_perfume_stores"])
    stores = [s for s in stores if s["name"] not in excluded]
    store_options = [{"uid": ALL_STORES_UID, "name": ALL_STORES_NAME}]
    store_options += sorted(
        ({"uid": s["uid"], "name": s["name"]} for s in stores),
        key=lambda s: s["name"].lower(),
    )
    store_options.append({"uid": UNALLOCATED_STORE_UID, "name": UNALLOCATED_STORE_NAME})
    return {
        "rows": result["rows"],
        "stores": store_options,
        "meta": {
            "period_days": days,
            "date_from": dt_from.isoformat(),
            "date_to": dt_to.isoformat(),
            "stock_as_of": result["as_of"],
            "stores_without_master": result["stores_without_master"],
            "stale_stores": result["stale_stores"],
            "excluded_perfume_stores": result["excluded_perfume_stores"],
            "test_skus_excluded": len(test_skus),
            "last_sale_lookback_days": LAST_SALE_LOOKBACK_DAYS,
            "shopify_costs_as_of": shopify.get("as_of"),
            "generated_at": datetime.utcnow().isoformat(),
        },
    }


def _read_category_snapshot() -> dict:
    try:
        return json.loads(CATEGORY_SNAPSHOT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


async def _load_categories() -> dict:
    """Warehouse categories, live or from the snapshot; {} if neither is available
    (the report still works, without categories)."""
    try:
        return {"by_sku": await fetch_categories(), "as_of": datetime.utcnow().isoformat()}
    except WarehouseUnavailable as e:
        snapshot = await asyncio.to_thread(_read_category_snapshot)
        if snapshot.get("by_sku"):
            return {"by_sku": snapshot["by_sku"], "as_of": snapshot.get("generated_at")}
        logger.warning("stock-coverage: categories unavailable: %s", e)
        return {}


def _row_key(row: dict) -> tuple:
    return (row["store_uid"], row["master_product_id"] or f"sku:{row['sku']}")


def _row_category(row: dict, categories: dict, stores_by_master: dict) -> str:
    """The warehouse's category for the SKU (shelf products); else the same taxonomy
    from the SKU code, the stores the product sells on, then its name."""
    skus = [s.strip().lower() for s in (row["sku"] or "").split(",") if s.strip()]
    for sku in skus:
        category = categories.get(sku)
        if category and category != WAREHOUSE_UNKNOWN:
            return category
    category = categorize(
        skus,
        stores_by_master.get(row["master_product_id"]) or (),
        row.get("product_name") or "",
    )
    if category:
        return category
    return DELISTED_CATEGORY if (row.get("stock") or 0) <= 0 else NO_CATEGORY


async def _build_combined() -> dict:
    current, long = await asyncio.gather(
        _cached(
            f"report|days={CURRENT_DAYS}",
            REPORT_TTL_SECONDS,
            lambda: _build_report(CURRENT_DAYS),
        ),
        _cached(
            f"report|days={DEAD_DAYS}", REPORT_TTL_SECONDS, lambda: _build_report(DEAD_DAYS)
        ),
    )
    category_source = await _cached(
        "categories", CATEGORIES_TTL_SECONDS, _load_categories
    )
    categories = category_source.get("by_sku") or {}
    rows = merge_periods(current["rows"], long["rows"], categories)
    ad_spend = await _cached("ad-spend", AD_SPEND_TTL_SECONDS, _load_ad_spend)
    ad_summary = annotate_ads(rows, ad_spend.get("rows") or [])
    return {
        **current,
        "rows": rows,
        "categories": sorted({r["category"] for r in rows}),
        "meta": {
            **current["meta"],
            "long_period_days": DEAD_DAYS,
            "categories_available": bool(categories),
            "categories_as_of": category_source.get("as_of"),
            "rows_without_category": sum(1 for r in rows if r["category"] == NO_CATEGORY),
            "ad_spend": {
                **ad_summary,
                "available": bool(ad_spend),
                "days": AD_SPEND_DAYS,
                "last_day": ad_spend.get("last_day"),
            },
        },
    }


def merge_periods(current_rows: list, long_rows: list, categories: dict) -> list:
    """The 30-day rows with the 90-day sales next to them, and each row's category."""
    long_by_key = {_row_key(r): r for r in long_rows}
    stores_by_master: dict = defaultdict(set)
    for r in (*current_rows, *long_rows):
        if r["master_product_id"] and r["store_uid"] not in (
            ALL_STORES_UID,
            UNALLOCATED_STORE_UID,
        ):
            stores_by_master[r["master_product_id"]].add(r["store_name"])
    rows = []
    for r in current_rows:
        long_row = long_by_key.get(_row_key(r)) or {}
        row = {
            **r,
            "sold_units_90": long_row.get("sold_units", 0.0),
            "category": _row_category(r, categories, stores_by_master),
        }
        if not r["sold_units"] and long_row.get("sold_units"):
            # Nothing in 30 days but sold in 90: the 90-day pace says how long it lasts.
            for key in (
                "velocity",
                "velocity_days",
                "coverage_days",
                "surplus_units",
                "surplus_value",
            ):
                row[key] = long_row[key]
            row["velocity_basis"] = "90"
        rows.append(row)
    # Sold in days 31-90 only and not in today's catalog (a product since removed): no
    # 30-day row, but its sales still belong in the 90-day column.
    current_keys = {_row_key(r) for r in current_rows}
    for key, long_row in long_by_key.items():
        if key not in current_keys and long_row.get("sold_units"):
            rows.append(
                {
                    **long_row,
                    "sold_units": 0.0,
                    "sold_units_90": long_row["sold_units"],
                    "velocity_basis": "90",
                    "category": _row_category(long_row, categories, stores_by_master),
                }
            )
    return rows


async def keep_warm():
    """Build the report at startup and keep it fresh, so a page open never waits for
    a cold build (each worker has its own cache)."""
    while True:
        try:
            await _cached("report|combined", REPORT_TTL_SECONDS, _build_combined)
        except Exception:
            logger.exception("stock-coverage: warm-up failed")
        await asyncio.sleep(WARM_INTERVAL_SECONDS)


PRODUCT_HISTORY_TTL_SECONDS = 120


@router.get("/stock-coverage/product-history")
async def product_history(
    sku: str = Query("", description="SKU-ul produsului (poate lipsi la masterii nelegați)"),
    master_product_id: Optional[str] = Query(None, description="Masterul stock-sync, dacă e legat"),
    limit: int = Query(60, ge=1, le=200),
):
    """Istoricul UNUI produs: unde stă pe raft, ce mișcări a avut stocul și cine l-a luat.

    Trei surse independente, fiecare cu poarta ei: dacă una tace, panoul arată restul și SPUNE
    care lipsește — altfel „nicio mișcare" s-ar confunda cu „n-am putut întreba".
      • locații  — rafturile din aplicația de scanare (inclusiv rebuturile, marcate `scrap`);
      • mișcări  — ledgerul stock-sync al masterului: recepții, inventare, corecții manuale,
                   reconcilieri din magazin, cu motiv, notă și autor;
      • ridicări — ce a luat depozitul de pe raft, pentru ce comandă și cine.
    """
    # Un rând din raport poate purta MAI MULTE SKU-uri: eticheta lui e `", ".join(skus)`
    # (computations.py), deci ajunge aici „oglinda, oglinda-acrilica". Căutat ca un singur SKU,
    # nu se găsea nici pe raft, nici la ridicări, iar panoul scria senin „nu e pe niciun raft"
    # pentru un produs cu 12.143 bucăți pe rafturi. Le despărțim și întrebăm pentru fiecare.
    skus = [x.strip().lower() for x in (sku or "").split(",") if x.strip()]

    # Masterul poate lipsi (ex. un produs deschis din Depozit, unde nu-l știe nimeni): îl
    # căutăm după SKU în raportul deja construit. Fără el nu există istoric de mișcări.
    if not master_product_id and skus:
        try:
            raport = await _cached("report|combined", REPORT_TTL_SECONDS, _build_combined)
            cerute = set(skus)
            master_product_id = next(
                (
                    r["master_product_id"]
                    for r in raport["rows"]
                    if r["master_product_id"]
                    and cerute & {s.strip().lower() for s in (r["sku"] or "").split(",")}
                ),
                None,
            )
        except Exception as e:
            logger.warning("product-history: nu s-a putut rezolva masterul pentru %s: %s", sku, e)

    cheie = f"product-history|{(sku or '').strip().lower()}|{master_product_id or '-'}|{limit}"

    async def _load():
        avertismente = []

        async def _sigur(nume, coroutine):
            try:
                return await coroutine
            except Exception as e:                      # o sursă căzută nu strică panoul
                logger.warning("product-history %s: %s", nume, e)
                avertismente.append(nume)
                return None

        async def _locatii_toate():
            out = []
            for s in skus:
                out += (await public_stock.locations_for(s)) or []
            return out

        async def _ridicari_toate():
            out = []
            for s in skus:
                d = await ws.product_picks(s)
                out += (d or {}).get("picks") or []
            out.sort(key=lambda x: str(x.get("at") or ""), reverse=True)
            return {"picks": out}

        # Un produs fără SKU (master nelegat de nicio listare) n-are ce căuta pe raft și
        # n-are ridicări — dar are istoric de stoc, care e tot ce vrei să vezi la el.
        locatii, miscari, ridicari = await asyncio.gather(
            _sigur("locatii", _locatii_toate()) if skus else asyncio.sleep(0, result=[]),
            _sigur("miscari", fetch_master_ledger(master_product_id, limit))
            if master_product_id
            else asyncio.sleep(0, result=[]),
            _sigur("ridicari", _ridicari_toate()) if skus else asyncio.sleep(0, result={"picks": []}),
        )
        picks = (ridicari or {}).get("picks") if isinstance(ridicari, dict) else None
        return {
            "sku": sku,
            "skus": skus,
            "locations": locatii or [],
            "movements": [
                {
                    "at": e.get("occurredAt"),
                    "delta": e.get("delta"),
                    "before": e.get("balanceBefore"),
                    "after": e.get("balanceAfter"),
                    "reason": e.get("reason"),
                    "note": e.get("note"),
                    "actor": e.get("actorName") or e.get("requestedBy") or e.get("actorType"),
                    "warehouse": e.get("warehouseName") or e.get("warehouseCode"),
                }
                for e in (miscari or [])
            ],
            "picks": picks or [],
            "unavailable": avertismente,
            "hasMaster": bool(master_product_id),
        }

    return await _cached(cheie, PRODUCT_HISTORY_TTL_SECONDS, _load)


@router.get("/stock-coverage")
async def get_stock_coverage(
    store_uids: Optional[str] = Query(None),
    master_product_id: Optional[str] = Query(None),
):
    """Stock coverage rows for `store_uids` (comma-separated; empty = the all-stores
    view). `master_product_id` returns that product's per-store breakdown instead."""
    try:
        report = await _cached("report|combined", REPORT_TTL_SECONDS, _build_combined)
    except StockSyncUnavailable as e:
        logger.error("stock-coverage: %s", e)
        raise HTTPException(503, detail=str(e))

    if master_product_id:
        rows = [
            r
            for r in report["rows"]
            if r["master_product_id"] == master_product_id
            and r["store_uid"] != ALL_STORES_UID
        ]
    else:
        wanted = {u.strip() for u in (store_uids or "").split(",") if u.strip()}
        wanted = wanted or {ALL_STORES_UID}
        rows = [r for r in report["rows"] if r["store_uid"] in wanted]
    return {**report, "rows": rows}
