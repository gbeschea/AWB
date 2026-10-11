"""
Order synchronization service.

Fetches orders from Frisbo and syncs them to local database.

Four-tier strategy (all tiers use Frisbo's `updated_at_start` filter so any
status change is picked up regardless of how old the order is):

  - "incremental"  : updated_at >= last_started_at - 15min (every 10 min)
                     Live coverage for today's active orders.
  - "recent_7d"    : updated_at >= now - 7 days  (every 20 min)
                     Close-to-live safety net for orders <=7 days old.
  - "window_30d"   : updated_at >= now - 30 days (every 2 h)
                     Catches late status changes on 7-30 day orders.
  - "deep_90d"     : updated_at >= now - 90 days (every 24 h)
                     Long-tail catch-up for very old orders that finally
                     deliver / return.

Legacy sync_type keys "3_day" and "45_day" are aliased to "recent_7d" and
"window_30d" respectively for backward compatibility with existing callers.

Manual triggers ("custom", "full") behave as before.

Implementation notes:
- Multi-org support: iterates through ALL organization tokens
- TRUE BATCH SAVING: saves every 100 orders as they are fetched
- PER-ORDER ERROR ISOLATION via savepoints: one bad order doesn't kill the batch
- COMPREHENSIVE LOGGING: per-store tracking, status changes, error grouping
"""

import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional, List
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.core.config import settings
from app.models import Order, OrderAwb, Store, SyncLog
from app.services.frisbo.client import FrisboClient
from app.services.frisbo.parser import parse_order
from app.core.status_classification import classify

logger = logging.getLogger(__name__)

# Terminal classification buckets — once an order reaches one of these it should
# never regress to a non-terminal status on a later sync (Frisbo can freeze/glitch a
# settled order back to waiting_for_courier; that's not reality). Also protects the
# statuses set by the stuck-order reconciliation against the courier feed.
_TERMINAL_CATS = {"delivered", "returned", "cancelled"}

# ── Tier window sizes (all use updated_at, so any status change is caught) ──
RECENT_SYNC_DAYS = 7  # Tier 2 "recent_7d": updated_at >= now - 7 days
WINDOW_SYNC_DAYS = 30  # Tier 3 "window_30d": updated_at >= now - 30 days
DEEP_SYNC_DAYS = 90  # Tier 4 "deep_90d": updated_at >= now - 90 days

# Tier 5/6 "recheck_*": re-fetch by CREATED_AT (not updated_at). The updated_at
# tiers only re-read an order when Frisbo bumps its updated_at; if a courier-status
# change is ingested WITHOUT bumping updated_at, those tiers never see it and the
# order goes stale. These tiers re-read every order created in the last N days and
# refresh its current aggregated_status regardless — the cure for stale orders.
#   • Tier 5 "recheck_30d" — last 30 days, frequent (every 3h): the hot window.
#   • Tier 6 "recheck_90d" — last 90 days, daily: covers the long tail (returns and
#     slow deliveries can resolve 30-90 days out). Beyond ~90 days orders are
#     effectively always terminal (verified against prod: Nov-2025 orders were
#     ~99.5% delivered/returned/cancelled), so 90 days is a sound practical bound.
RECHECK_BY_CREATED_DAYS = 30
RECHECK_BY_CREATED_DAYS_LONG = 90

# Legacy sync_type keys are mapped to the new tier they best represent. Callers
# still sending "3_day" or "45_day" continue to work; the SyncLog records the
# canonical (post-alias) sync_type.
SYNC_TYPE_ALIASES = {"3_day": "recent_7d", "45_day": "window_30d"}

INCREMENTAL_OVERLAP_MIN = (
    15  # Overlap buffer for incremental sync (handles clock drift + long syncs)
)
INCREMENTAL_FALLBACK_DAYS = (
    2  # If no previous sync: fall back to this many days (not 45, to stay fast)
)
BATCH_SIZE = 100  # Save to database every N orders


async def get_last_successful_sync_time() -> Optional[datetime]:
    """
    Get the *started_at* timestamp of the last successful order sync.

    CRITICAL: We use started_at (not completed_at) as the reference point so
    that orders updated *during* a long sync run are captured in the next
    incremental.  Using completed_at creates a gap window equal to the sync
    duration.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(SyncLog.started_at)
            .where(SyncLog.status == "completed")
            .where(
                SyncLog.sync_type.in_(
                    [
                        "incremental",
                        "recent_7d",
                        "window_30d",
                        "deep_90d",
                        "recheck_30d",
                        "recheck_90d",
                        "full",
                        "custom",
                        # Legacy keys kept for historic rows (pre-alias rename)
                        "3_day",
                        "45_day",
                    ]
                )
            )
            .order_by(SyncLog.started_at.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        return row


async def sync_orders(
    sync_id: Optional[int] = None,
    full_sync: bool = False,
    sync_type: str = "window_30d",
    store_uids: Optional[List[str]] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    """
    Synchronize orders from Frisbo organizations to local database.

    Iterates through every org token in FRISBO_ORG_TOKENS.
    For each org, does streaming batch fetch + save (100 orders at a time).

    Sync types (all use updated_at_start so status changes are always caught):
        - "incremental": updated_at >= last_sync.started_at - 15min (fast, seconds)
        - "recent_7d":   updated_at >= now - 7 days (absolute, independent of last sync)
                         Close-to-live safety net for orders <=7 days old.
                         Legacy alias: "3_day".
        - "window_30d":  updated_at >= now - 30 days
                         Catches late status changes on 7-30 day orders.
                         Legacy alias: "45_day" (was buggy — used created_at).
        - "deep_90d":    updated_at >= now - 90 days
                         Long-tail catch-up for very old orders.
        - "full":        No date filters — all orders (slow, manual only)
        - "custom":      User-specified created_at date range
    """
    # Normalize legacy sync_type aliases so the rest of the function (and the
    # SyncLog row) speak the canonical names.
    sync_type = SYNC_TYPE_ALIASES.get(sync_type, sync_type)

    sync_start_time = time.time()

    org_tokens = settings.get_org_tokens()
    if not org_tokens:
        logger.error(
            "📦 No Frisbo org tokens configured! Check FRISBO_ORG_TOKENS in .env"
        )
        return

    org_names = [o.get("name", f"org-{i}") for i, o in enumerate(org_tokens)]
    logger.info(
        f"📦 ═══ SYNC START ({sync_type}) ═══ {len(org_tokens)} orgs: {', '.join(org_names)}"
    )

    # ── Tracking structures for detailed reporting ──
    per_org_stats = {}  # {org_name: {fetched, new, updated, skipped}}
    per_store_stats = defaultdict(
        lambda: {"new": 0, "updated": 0}
    )  # {store_uid: counters}
    status_changes = []  # [(order_number, old_status, new_status)]
    error_groups = defaultdict(list)  # {error_type: [order_uid, ...]}
    new_stores_discovered = []  # store_uids seen for the first time

    async with AsyncSessionLocal() as db:
        try:
            # Update sync log to running
            sync_log = None
            if sync_id:
                result = await db.execute(select(SyncLog).where(SyncLog.id == sync_id))
                sync_log = result.scalar_one_or_none()

            if not sync_log:
                sync_log = SyncLog(status="running", sync_type=sync_type)
                db.add(sync_log)
                await db.flush()

            # Record sync metadata in the log
            sync_log.sync_type = sync_type
            if store_uids:
                sync_log.store_uids = store_uids
            if date_from:
                try:
                    parsed_dt = datetime.fromisoformat(date_from.replace("Z", "+00:00"))
                    sync_log.date_from = parsed_dt.replace(tzinfo=None)
                except Exception:
                    pass
            if date_to:
                try:
                    parsed_dt = datetime.fromisoformat(date_to.replace("Z", "+00:00"))
                    sync_log.date_to = parsed_dt.replace(tzinfo=None)
                except Exception:
                    pass
            await db.commit()

            # ── Determine API filters based on sync type ──
            created_at_start = None
            created_at_end = None
            updated_at_start = None

            if sync_type == "incremental":
                last_sync_time = await get_last_successful_sync_time()
                if last_sync_time:
                    # Subtract overlap buffer to cover any clock skew / long sync durations
                    incremental_since = last_sync_time - timedelta(
                        minutes=INCREMENTAL_OVERLAP_MIN
                    )
                    updated_at_start = incremental_since.isoformat()
                    sync_log.incremental_since = incremental_since
                    logger.info(
                        f"📦 INCREMENTAL: updated_at >= {updated_at_start} "
                        f"(last started_at: {last_sync_time.isoformat()}, "
                        f"overlap: {INCREMENTAL_OVERLAP_MIN}min)"
                    )
                else:
                    # First ever sync — use a short fallback window (not 45 days) to stay fast
                    logger.warning(
                        f"📦 INCREMENTAL: No previous sync found → "
                        f"falling back to {INCREMENTAL_FALLBACK_DAYS}-day updated_at window"
                    )
                    cutoff_date = datetime.utcnow() - timedelta(
                        days=INCREMENTAL_FALLBACK_DAYS
                    )
                    updated_at_start = cutoff_date.isoformat()
                    sync_log.incremental_since = cutoff_date

            elif sync_type == "recent_7d":
                # Tier 2: absolute 7-day updated_at window — does NOT depend on last sync time.
                # Close-to-live safety net for orders <=7 days old. Even after a server restart
                # or a run of failed incrementals, this guarantees recent orders (new AND
                # status-changed) are never more than ~20 min stale.
                cutoff_recent = datetime.utcnow() - timedelta(days=RECENT_SYNC_DAYS)
                updated_at_start = cutoff_recent.isoformat()
                sync_log.incremental_since = cutoff_recent
                logger.info(
                    f"📦 RECENT_7D: updated_at >= {updated_at_start} "
                    f"(absolute {RECENT_SYNC_DAYS}-day window, independent of last sync)"
                )

            elif sync_type == "window_30d":
                # Tier 3: absolute 30-day updated_at window. Catches late status changes on
                # 7-30 day orders that the recent_7d job won't see (e.g., an order that
                # finally delivers after 20 days in transit). Uses updated_at, NOT created_at
                # — the previous "45_day" job filtered on created_at which silently dropped
                # any order whose creation date was outside the window even if its status
                # had just changed.
                cutoff_window = datetime.utcnow() - timedelta(days=WINDOW_SYNC_DAYS)
                updated_at_start = cutoff_window.isoformat()
                sync_log.incremental_since = cutoff_window
                logger.info(
                    f"📦 WINDOW_30D: updated_at >= {updated_at_start} "
                    f"(absolute {WINDOW_SYNC_DAYS}-day window)"
                )

            elif sync_type == "deep_90d":
                # Tier 4: absolute 90-day updated_at window. Long-tail catch-up for very
                # old orders that finally deliver or return. Runs daily.
                cutoff_deep = datetime.utcnow() - timedelta(days=DEEP_SYNC_DAYS)
                updated_at_start = cutoff_deep.isoformat()
                sync_log.incremental_since = cutoff_deep
                logger.info(
                    f"📦 DEEP_90D: updated_at >= {updated_at_start} "
                    f"(absolute {DEEP_SYNC_DAYS}-day window)"
                )

            elif sync_type == "recheck_30d":
                # Tier 5: re-fetch by CREATED_AT (not updated_at) so recent orders
                # are re-read and their current aggregated_status refreshed EVEN IF
                # Frisbo never bumped updated_at on a courier-status change — the
                # root cause of stale orders. Complements the updated_at tiers
                # (which still catch status changes on older orders when bumped).
                cutoff_recheck = datetime.utcnow() - timedelta(
                    days=RECHECK_BY_CREATED_DAYS
                )
                created_at_start = cutoff_recheck.isoformat()
                sync_log.incremental_since = cutoff_recheck
                logger.info(
                    f"📦 RECHECK_30D: created_at >= {created_at_start} "
                    f"(re-reads current status regardless of updated_at)"
                )

            elif sync_type == "recheck_90d":
                # Tier 6: same created_at recheck as Tier 5 but a wider 90-day window,
                # run daily. Cures staleness on the long tail — orders that change
                # status (final delivery, return) 30-90 days after creation without
                # Frisbo bumping updated_at, which both the updated_at tiers and the
                # 30-day recheck would miss.
                cutoff_recheck = datetime.utcnow() - timedelta(
                    days=RECHECK_BY_CREATED_DAYS_LONG
                )
                created_at_start = cutoff_recheck.isoformat()
                sync_log.incremental_since = cutoff_recheck
                logger.info(
                    f"📦 RECHECK_90D: created_at >= {created_at_start} "
                    f"(long-tail status refresh regardless of updated_at)"
                )

            elif sync_type == "custom" and date_from:
                created_at_start = date_from
                created_at_end = date_to
                logger.info(
                    f"📦 CUSTOM: created_at {date_from} → {date_to or 'now'}"
                    + (f" stores={store_uids}" if store_uids else "")
                )

            elif not full_sync:
                # Fallback for an unknown sync_type that isn't full_sync: behave like
                # window_30d so we never silently fetch nothing.
                cutoff_window = datetime.utcnow() - timedelta(days=WINDOW_SYNC_DAYS)
                updated_at_start = cutoff_window.isoformat()
                sync_log.incremental_since = cutoff_window
                logger.warning(
                    f"📦 UNKNOWN sync_type={sync_type!r} — defaulting to window_30d "
                    f"(updated_at >= {updated_at_start})"
                )
            else:
                logger.info("📦 FULL: No date filters — fetching ALL orders")

            await db.commit()

            # Global counters
            total_fetched = 0
            new_count = 0
            updated_count = 0
            skipped_count = 0

            # ─── Pre-load known store UIDs for new store detection ───
            known_stores_result = await db.execute(select(Store.uid))
            known_store_uids = {row[0] for row in known_stores_result.fetchall()}
            logger.info(f"📦 Known stores in DB: {len(known_store_uids)}")

            # ═══════════════════════════════════════════════════════
            # Iterate through ALL organizations
            # ═══════════════════════════════════════════════════════
            for org_idx, org in enumerate(org_tokens):
                org_name = org.get("name", f"org-{org_idx}")
                org_token = org.get("token", "")

                if not org_token:
                    logger.warning(f"📦 [{org_name}] SKIP — empty token")
                    continue

                org_start_time = time.time()
                logger.info(
                    f"📦 ──── ORG {org_idx + 1}/{len(org_tokens)}: {org_name} ────"
                )

                client = FrisboClient(token=org_token, org_name=org_name)

                org_fetched = 0
                org_new = 0
                org_updated = 0
                org_skipped = 0
                org_stores_seen = set()
                skip = 0
                api_errors = 0

                # Per-org filter set: only orders whose store_uid is in `store_uids`
                # will be processed. We do NOT pass `store_uids[]` to Frisbo — empirically
                # Frisbo's API returns ~0 orders when many UIDs are in the array filter
                # (custom sync log id=2875 had 17 UIDs and fetched only 2 orders total).
                # Each org token is already scoped to its own org, so the filter is
                # redundant for narrowing on the wire; doing it Python-side is safer.
                store_filter_set = set(store_uids) if store_uids else None

                while True:
                    # ── Fetch one batch from Frisbo API ──
                    try:
                        result = await client.search_orders(
                            skip=skip,
                            limit=BATCH_SIZE,
                            store_uids=None,  # Python-side filter — see comment above.
                            created_at_start=created_at_start,
                            created_at_end=created_at_end,
                            updated_at_start=updated_at_start,
                        )
                    except Exception as e:
                        api_errors += 1
                        error_type = type(e).__name__
                        logger.error(
                            f"📦 [{org_name}] API ERROR at skip={skip}: [{error_type}] {e}"
                        )
                        error_groups[f"API:{error_type}"].append(
                            f"{org_name}@skip={skip}"
                        )
                        if api_errors >= 3:
                            logger.error(
                                f"📦 [{org_name}] 3 consecutive API errors — abandoning org"
                            )
                            break
                        skip += BATCH_SIZE
                        continue

                    api_errors = 0  # Reset on success

                    # ── Parse response ──
                    orders_batch = []
                    if isinstance(result, dict):
                        if result.get("success") is False:
                            errors = result.get("errors", [])
                            logger.error(
                                f"📦 [{org_name}] Frisbo returned success=false: {errors}"
                            )
                            error_groups["API:success=false"].append(
                                f"{org_name}: {errors}"
                            )
                            break
                        data = result.get("data", {})
                        if isinstance(data, dict):
                            orders_batch = data.get("orders", [])
                        elif isinstance(data, list):
                            orders_batch = data

                    if not orders_batch:
                        logger.info(
                            f"📦 [{org_name}] End of data at skip={skip}. Org total: {org_fetched}"
                        )
                        break

                    batch_fetched = len(orders_batch)
                    org_fetched += batch_fetched
                    total_fetched += batch_fetched

                    # ══════════════════════════════════════════════════
                    # Process batch with PER-ORDER SAVEPOINT isolation
                    # ══════════════════════════════════════════════════
                    batch_new = 0
                    batch_updated = 0
                    batch_skipped = 0

                    for raw_order in orders_batch:
                        if not isinstance(raw_order, dict):
                            continue

                        order_uid = raw_order.get("uid", "unknown")
                        order_ref = raw_order.get("reference", order_uid)

                        try:
                            # Use a savepoint so we can rollback just this order on failure
                            async with db.begin_nested():
                                parsed = parse_order(raw_order)

                                # Validate required fields
                                if not parsed.get("uid"):
                                    logger.warning(
                                        f"📦 [{org_name}] Order has no UID — skipping: ref={order_ref}"
                                    )
                                    batch_skipped += 1
                                    skipped_count += 1
                                    continue

                                store_uid = parsed["store_uid"]
                                org_stores_seen.add(store_uid)

                                # Python-side store filter (replaces Frisbo's buggy
                                # store_uids[] array filter — see comment at the
                                # while-loop preamble).
                                if (
                                    store_filter_set
                                    and store_uid not in store_filter_set
                                ):
                                    batch_skipped += 1
                                    skipped_count += 1
                                    continue

                                # ── Ensure store exists ──
                                if store_uid and store_uid not in known_store_uids:
                                    await ensure_store_exists(db, store_uid)
                                    known_store_uids.add(store_uid)
                                    new_stores_discovered.append(store_uid)
                                    logger.info(
                                        f"📦 [{org_name}] NEW STORE discovered: {store_uid}"
                                    )

                                # ── Check if order exists ──
                                existing_result = await db.execute(
                                    select(Order).where(Order.uid == parsed["uid"])
                                )
                                existing = existing_result.scalar_one_or_none()

                                if existing:
                                    # ── UPDATE existing order ──
                                    # Track status changes for logging
                                    old_agg = existing.aggregated_status
                                    new_agg = parsed.get("aggregated_status")
                                    old_shipment = existing.shipment_status
                                    new_shipment = parsed.get("shipment_status")

                                    if (
                                        old_agg != new_agg
                                        or old_shipment != new_shipment
                                    ):
                                        status_changes.append(
                                            (
                                                existing.order_number,
                                                f"agg={old_agg or '?'}/ship={old_shipment or '?'}",
                                                f"agg={new_agg or '?'}/ship={new_shipment or '?'}",
                                            )
                                        )

                                    existing.tracking_number = (
                                        parsed.get("tracking_number")
                                        or existing.tracking_number
                                    )
                                    existing.awb_pdf_url = (
                                        parsed.get("awb_pdf_url")
                                        or existing.awb_pdf_url
                                    )
                                    existing.courier_name = (
                                        parsed.get("courier_name")
                                        or existing.courier_name
                                    )
                                    existing.shipment_uid = (
                                        parsed.get("shipment_uid")
                                        or existing.shipment_uid
                                    )
                                    existing.fulfillment_status = parsed[
                                        "fulfillment_status"
                                    ]
                                    existing.shipment_status = (
                                        new_shipment or existing.shipment_status
                                    )
                                    # Don't DOWNGRADE a settled order: if it's already
                                    # terminal (delivered/returned/cancelled) and Frisbo
                                    # now sends a non-terminal status, keep the terminal
                                    # one. Terminal→terminal and any→terminal still apply.
                                    if new_agg and not (
                                        classify(existing.aggregated_status)
                                        in _TERMINAL_CATS
                                        and classify(new_agg) not in _TERMINAL_CATS
                                    ):
                                        existing.aggregated_status = new_agg
                                    # Auto-clear stale courier alert
                                    if (
                                        existing.waiting_for_courier_since
                                        and new_agg
                                        and new_agg != "waiting_for_courier"
                                    ):
                                        existing.waiting_for_courier_since = None
                                        logger.debug(
                                            f"📦 [{org_name}] Cleared waiting_for_courier for {existing.order_number}"
                                        )
                                    existing.fulfilled_at = (
                                        parsed.get("fulfilled_at")
                                        or existing.fulfilled_at
                                    )
                                    existing.total_price = (
                                        parsed.get("total_price")
                                        or existing.total_price
                                    )
                                    existing.subtotal_price = (
                                        parsed.get("subtotal_price")
                                        or existing.subtotal_price
                                    )
                                    existing.total_discounts = (
                                        parsed.get("total_discounts")
                                        or existing.total_discounts
                                    )
                                    existing.currency = (
                                        parsed.get("currency") or existing.currency
                                    )
                                    existing.payment_gateway = (
                                        parsed.get("payment_gateway")
                                        or existing.payment_gateway
                                    )
                                    # Only overwrite line_items when the new payload
                                    # actually HAS items. The parser returns [] (never
                                    # None) for an absent/empty payload, so the old
                                    # `is not None` guard let a partial Frisbo response
                                    # wipe good line_items to [] → COGS silently became
                                    # 0 (599 delivered orders were hit this way). A
                                    # genuinely item-less order is a data anomaly we
                                    # prefer to leave as-is rather than zero a good one.
                                    if parsed.get("line_items"):
                                        existing.line_items = parsed["line_items"]
                                        existing.item_count = parsed["item_count"]
                                        existing.unique_sku_count = parsed[
                                            "unique_sku_count"
                                        ]
                                    # Tags/note: coalesce so a response that omits
                                    # them never wipes existing values, but a
                                    # populated list/string refreshes them.
                                    existing.tags = parsed.get("tags") or existing.tags
                                    existing.note = parsed.get("note") or existing.note
                                    # Only stamp synced_at when something ELSE actually
                                    # changed. Stamping unconditionally made every row
                                    # dirty on every pass, so the 7d/30d tiers physically
                                    # rewrote every recent order dozens of times a day
                                    # (~1 KB line_items each) - 145k UPDATEs / 10 min on
                                    # 738k rows. The dead copies could never be returned
                                    # to the OS: orders reached 94 GB for 1 GB of live
                                    # data, filled the disk, and Postgres went down
                                    # (2026-08-24). An unchanged order is now a no-op,
                                    # and the "updated" counters mean what they say.
                                    if db.is_modified(existing):
                                        existing.synced_at = datetime.utcnow()
                                        updated_count += 1
                                        org_updated += 1
                                        batch_updated += 1
                                        per_store_stats[store_uid]["updated"] += 1
                                    order_obj = existing
                                else:
                                    # ── CREATE new order ──
                                    order_obj = Order(
                                        uid=parsed["uid"],
                                        order_number=parsed["order_number"],
                                        store_uid=parsed["store_uid"],
                                        customer_name=parsed["customer_name"],
                                        customer_email=parsed.get("customer_email"),
                                        shipping_address=parsed.get("shipping_address"),
                                        line_items=parsed["line_items"],
                                        item_count=parsed["item_count"],
                                        unique_sku_count=parsed["unique_sku_count"],
                                        tracking_number=parsed.get("tracking_number"),
                                        courier_name=parsed.get("courier_name"),
                                        awb_pdf_url=parsed.get("awb_pdf_url"),
                                        shipment_uid=parsed.get("shipment_uid"),
                                        fulfillment_status=parsed["fulfillment_status"],
                                        financial_status=parsed.get(
                                            "financial_status", "pending"
                                        ),
                                        shipment_status=parsed.get("shipment_status"),
                                        aggregated_status=parsed.get(
                                            "aggregated_status"
                                        ),
                                        frisbo_created_at=parsed.get(
                                            "frisbo_created_at"
                                        ),
                                        fulfilled_at=parsed.get("fulfilled_at"),
                                        total_price=parsed.get("total_price"),
                                        subtotal_price=parsed.get("subtotal_price"),
                                        total_discounts=parsed.get("total_discounts"),
                                        currency=parsed.get("currency", "RON"),
                                        payment_gateway=parsed.get("payment_gateway"),
                                        tags=parsed.get("tags"),
                                        note=parsed.get("note"),
                                        synced_at=datetime.utcnow(),
                                    )
                                    db.add(order_obj)
                                    new_count += 1
                                    org_new += 1
                                    batch_new += 1
                                    per_store_stats[store_uid]["new"] += 1
                                    logger.debug(
                                        f"📦 [{org_name}] NEW order: {parsed['order_number']} store={store_uid} status={parsed.get('aggregated_status')}"
                                    )

                                # ── Upsert AWBs ──
                                all_awbs = parsed.get("all_awbs", [])
                                if all_awbs:
                                    await db.flush()
                                    existing_awbs_result = await db.execute(
                                        select(OrderAwb).where(
                                            OrderAwb.order_id == order_obj.id
                                        )
                                    )
                                    existing_awbs = {
                                        oa.tracking_number: oa
                                        for oa in existing_awbs_result.scalars().all()
                                    }

                                    for awb_data in all_awbs:
                                        tn = awb_data["tracking_number"]
                                        if not tn:
                                            continue  # Skip AWBs with empty tracking numbers
                                        if tn in existing_awbs:
                                            ea = existing_awbs[tn]
                                            ea.courier_name = (
                                                awb_data.get("courier_name")
                                                or ea.courier_name
                                            )
                                            ea.awb_type = (
                                                awb_data.get("awb_type") or ea.awb_type
                                            )
                                            ea.shipment_uid = (
                                                awb_data.get("shipment_uid")
                                                or ea.shipment_uid
                                            )
                                            ea.awb_pdf_url = (
                                                awb_data.get("awb_pdf_url")
                                                or ea.awb_pdf_url
                                            )
                                            ea.awb_pdf_format = (
                                                awb_data.get("awb_pdf_format")
                                                or ea.awb_pdf_format
                                            )
                                            ea.shipment_status = (
                                                awb_data.get("shipment_status")
                                                or ea.shipment_status
                                            )
                                            ea.shipment_status_date = (
                                                awb_data.get("shipment_status_date")
                                                or ea.shipment_status_date
                                            )
                                            ea.shipment_events = (
                                                awb_data.get("shipment_events")
                                                or ea.shipment_events
                                            )
                                            ea.is_return_label = (
                                                awb_data.get("is_return_label")
                                                if awb_data.get("is_return_label")
                                                is not None
                                                else ea.is_return_label
                                            )
                                            ea.is_redirect_label = (
                                                awb_data.get("is_redirect_label")
                                                if awb_data.get("is_redirect_label")
                                                is not None
                                                else ea.is_redirect_label
                                            )
                                            ea.paid_by = (
                                                awb_data.get("paid_by") or ea.paid_by
                                            )
                                            ea.cod_value = (
                                                awb_data.get("cod_value")
                                                if awb_data.get("cod_value") is not None
                                                else ea.cod_value
                                            )
                                            ea.cod_currency = (
                                                awb_data.get("cod_currency")
                                                or ea.cod_currency
                                            )
                                            ea.shipment_created_at = (
                                                awb_data.get("shipment_created_at")
                                                or ea.shipment_created_at
                                            )
                                        else:
                                            new_awb = OrderAwb(
                                                order_id=order_obj.id,
                                                tracking_number=tn,
                                                courier_name=awb_data.get(
                                                    "courier_name"
                                                ),
                                                awb_type=awb_data.get(
                                                    "awb_type", "outbound"
                                                ),
                                                shipment_uid=awb_data.get(
                                                    "shipment_uid"
                                                ),
                                                awb_pdf_url=awb_data.get("awb_pdf_url"),
                                                awb_pdf_format=awb_data.get(
                                                    "awb_pdf_format"
                                                ),
                                                shipment_status=awb_data.get(
                                                    "shipment_status"
                                                ),
                                                shipment_status_date=awb_data.get(
                                                    "shipment_status_date"
                                                ),
                                                is_return_label=awb_data.get(
                                                    "is_return_label", False
                                                ),
                                                is_redirect_label=awb_data.get(
                                                    "is_redirect_label", False
                                                ),
                                                paid_by=awb_data.get("paid_by"),
                                                cod_value=awb_data.get("cod_value"),
                                                cod_currency=awb_data.get(
                                                    "cod_currency"
                                                ),
                                                shipment_created_at=awb_data.get(
                                                    "shipment_created_at"
                                                ),
                                                shipment_events=awb_data.get(
                                                    "shipment_events"
                                                ),
                                                data_source="frisbo_sync",
                                                created_at=datetime.utcnow(),
                                            )
                                            db.add(new_awb)

                        except Exception as order_err:
                            # Savepoint auto-rolled back — only THIS order is lost
                            error_type = type(order_err).__name__
                            error_msg = str(order_err)[:200]
                            logger.warning(
                                f"📦 [{org_name}] SKIP {order_ref} ({order_uid}): [{error_type}] {error_msg}"
                            )
                            error_groups[f"Order:{error_type}"].append(f"{order_ref}")
                            batch_skipped += 1
                            skipped_count += 1
                            org_skipped += 1
                            continue

                    # ── COMMIT THIS BATCH ──
                    try:
                        sync_log.orders_fetched = total_fetched
                        sync_log.orders_new = new_count
                        sync_log.orders_updated = updated_count
                        sync_log.orders_skipped = skipped_count
                        await db.commit()
                    except Exception as batch_err:
                        error_type = type(batch_err).__name__
                        logger.error(
                            f"📦 [{org_name}] BATCH COMMIT FAILED skip={skip}: "
                            f"[{error_type}] {batch_err}"
                        )
                        error_groups[f"Batch:{error_type}"].append(
                            f"{org_name}@skip={skip}"
                        )
                        await db.rollback()
                        # Re-fetch sync_log after rollback — the object is now detached
                        try:
                            sl_result = await db.execute(
                                select(SyncLog).where(SyncLog.id == sync_log.id)
                            )
                            refreshed = sl_result.scalar_one_or_none()
                            if refreshed:
                                sync_log = refreshed
                        except Exception:
                            pass
                        skip += BATCH_SIZE
                        continue

                    # Batch log
                    logger.info(
                        f"📦 [{org_name}] BATCH {skip // BATCH_SIZE + 1}: "
                        f"+{batch_new} new, ~{batch_updated} upd"
                        f"{f', ✗{batch_skipped} skip' if batch_skipped else ''} | "
                        f"org={org_fetched} total={total_fetched}"
                    )

                    if batch_fetched < BATCH_SIZE:
                        break

                    skip += BATCH_SIZE

                # ── Org summary ──
                org_elapsed = time.time() - org_start_time
                per_org_stats[org_name] = {
                    "fetched": org_fetched,
                    "new": org_new,
                    "updated": org_updated,
                    "skipped": org_skipped,
                    "stores": list(org_stores_seen),
                    "elapsed": round(org_elapsed, 1),
                }
                logger.info(
                    f"📦 [{org_name}] DONE: {org_fetched} fetched, +{org_new} new, "
                    f"~{org_updated} upd, ✗{org_skipped} skip | "
                    f"stores({len(org_stores_seen)}): {', '.join(sorted(org_stores_seen)[:10])}"
                    f"{'...' if len(org_stores_seen) > 10 else ''} | {org_elapsed:.1f}s"
                )

            # ═══════════════════════════════════════════════════════
            # SYNC COMPLETE — Final Summary
            # ═══════════════════════════════════════════════════════
            sync_log.orders_fetched = total_fetched
            sync_log.orders_new = new_count
            sync_log.orders_updated = updated_count
            sync_log.orders_skipped = skipped_count
            sync_log.status = "completed"
            sync_log.completed_at = datetime.utcnow()
            await db.commit()

            # Invalidate the analytics TTL cache so reports reflect the fresh data
            # immediately after a sync (instead of serving cached numbers for the TTL).
            if new_count or updated_count:
                from app.core.analytics_cache import cache_clear

                cache_clear()

            total_elapsed = time.time() - sync_start_time

            # ── Summary logs ──
            logger.info(f"📦 ═══ SYNC COMPLETE ({sync_type}) ═══ {total_elapsed:.1f}s")
            logger.info(
                f"📦 TOTALS: {total_fetched} fetched | +{new_count} new | ~{updated_count} updated | ✗{skipped_count} skipped"
            )

            # Per-org breakdown
            if len(per_org_stats) > 1:
                for oname, ostats in per_org_stats.items():
                    logger.info(
                        f"📦   org={oname}: {ostats['fetched']} orders, {len(ostats['stores'])} stores, {ostats['elapsed']}s"
                    )

            # Per-store breakdown (top stores by activity)
            if per_store_stats:
                sorted_stores = sorted(
                    per_store_stats.items(),
                    key=lambda x: x[1]["new"] + x[1]["updated"],
                    reverse=True,
                )
                top_n = min(15, len(sorted_stores))
                for store_uid, counts in sorted_stores[:top_n]:
                    if counts["new"] > 0 or counts["updated"] > 0:
                        logger.info(
                            f"📦   store={store_uid}: +{counts['new']} new, ~{counts['updated']} updated"
                        )

            # Status change log
            if status_changes:
                logger.info(
                    f"📦 STATUS CHANGES: {len(status_changes)} orders changed status"
                )
                for order_num, old_s, new_s in status_changes[:20]:
                    logger.info(f"📦   {order_num}: {old_s} → {new_s}")
                if len(status_changes) > 20:
                    logger.info(f"📦   ... and {len(status_changes) - 20} more")

            # New stores
            if new_stores_discovered:
                logger.info(
                    f"📦 NEW STORES: {len(new_stores_discovered)} discovered: {', '.join(new_stores_discovered)}"
                )

            # Error summary (grouped)
            if error_groups:
                logger.warning(
                    f"📦 ═══ ERROR SUMMARY ({sum(len(v) for v in error_groups.values())} total) ═══"
                )
                for err_type, affected in sorted(
                    error_groups.items(), key=lambda x: -len(x[1])
                ):
                    sample = affected[:5]
                    logger.warning(
                        f"📦   [{err_type}] ({len(affected)}x): {', '.join(sample)}"
                        + (
                            f"... +{len(affected) - 5} more"
                            if len(affected) > 5
                            else ""
                        )
                    )
            else:
                logger.info("📦 No errors during sync ✓")

        except Exception as e:
            total_elapsed = time.time() - sync_start_time
            logger.error(
                f"📦 ═══ SYNC FAILED after {total_elapsed:.1f}s ═══ {type(e).__name__}: {e}"
            )
            import traceback

            traceback.print_exc()
            try:
                await db.rollback()
            except Exception:
                pass
            if sync_log:
                try:
                    sync_log.status = "failed"
                    sync_log.error_message = str(e)[:1000]
                    sync_log.completed_at = datetime.utcnow()
                    await db.commit()
                except Exception:
                    logger.error("📦 Could not update sync_log after failure")
            raise


async def ensure_store_exists(db: AsyncSession, store_uid: str):
    """Ensure a store exists in the database, create if not."""
    if not store_uid:
        return

    result = await db.execute(select(Store).where(Store.uid == store_uid))
    if not result.scalar_one_or_none():
        store = Store(
            uid=store_uid,
            name=store_uid,  # Will be updated from API response
            color_code=generate_color_from_uid(store_uid),
        )
        db.add(store)
        await db.flush()


def generate_color_from_uid(uid: str) -> str:
    """Generate a consistent color from a UID string."""
    colors = [
        "#6366f1",
        "#8b5cf6",
        "#ec4899",
        "#f43f5e",
        "#ef4444",
        "#f97316",
        "#eab308",
        "#22c55e",
        "#14b8a6",
        "#06b6d4",
    ]
    hash_val = sum(ord(c) for c in uid)
    return colors[hash_val % len(colors)]
