"""
Pure computations for the per-store stock coverage report ("Stoc & Viteză").

No DB / HTTP here — the endpoint feeds in the AWB stores, the stock-sync snapshot,
the AWB catalog for stores stock-sync doesn't manage, per-(store, SKU) sales, last
sale dates and SKU costs.

Two views share one row shape:
  • per store — (AWB store, master product); the stock is what that store holds;
  • "Toate magazinele" — one row per master product; stock = master total, sales
    and last sale taken across every store.
Every row carries ONE stock, ONE coverage and a status, so "what isn't selling"
reads straight off the table.
"""

import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

UNALLOCATED_STORE_UID = "__nealocat__"
UNALLOCATED_STORE_NAME = "Nealocat (pe niciun magazin)"
ALL_STORES_UID = "__toate__"
ALL_STORES_NAME = "Toate magazinele"

BUCHAREST = ZoneInfo("Europe/Bucharest")

SOON_DAYS = 14
SLOW_DAYS = 180
VERY_SLOW_DAYS = 365
# "Surplus": what is still unsold after this long at today's pace (= the "lent" line).
SURPLUS_DAYS = SLOW_DAYS
# Dead stock: nothing sold for this long, whatever period the page is showing
# (same 90-day rule as the team's produse-fara-ads "MORT" verdict).
DEAD_DAYS = 90
# A store with no order for this long has unreliable sales history (produse-fara-ads).
STALE_STORE_DAYS = 7
# stock-sync went live 21.07 and seeded every master's opening stock until ~26.07;
# a 0 → stock jump before this is the seeding, not goods arriving.
LEDGER_SEEDING_END = "2026-07-27"

# Perfumes are out of scope for this report.
PERFUME_STORE_NAMES = {"esteban.ro", "georgetalent.ro", "nubra", "labnoir.ro", "duppo.md"}
PERFUME_NAME_RE = re.compile(
    # "parfum" only leading or as "apă de parfum": "Sita Odorizanta …, Parfum 45 Zile"
    # is a urinal screen, not a perfume.
    r"inspired by|inspirat (de|din)|^parfum\b|ap[aă] de parfum|\bperfume"
    r"|eau de (parfum|toilette)|\bzeylin\b|l'essence|\bessence no\b|^no\. ?\d+\b",
    re.IGNORECASE,
)
# Placeholder SKUs (seeded "infinite" stock) — excluded like in produse-fara-ads.
PLACEHOLDER_SKU_RE = re.compile(r"surpriza|mystery|cutie-cadou|^test", re.IGNORECASE)
NEGATIVE_STATUSES = {"mort", "nu_se_vinde", "lent", "foarte_lent"}
YEAR_DAYS = 365
# Goods arriving (Gigi's review): a receipt (RECEIVING, or a note saying "recepție" /
# "livrare") or ANY stock going from 0 to N — the real C54-C56 goods were booked on
# 23.09 as "Inventar" — net of the manual corrections that undo it within 48 hours
# (C55/C56 were booked at 11:27 on 24.09 and cancelled at 12:00 as double counting).
# Only the arrived units are "nou": the stock already there keeps its verdict, and
# goods that haven't sold for NEW_GOODS_GRACE_DAYS after arriving are judged as usual.
MIN_RECEIPT_UNITS = 10
ARRIVAL_UNDO_HOURS = 48
NEW_GOODS_GRACE_DAYS = 21
RECEIPT_NOTE_RE = re.compile(r"recep[tț]i|livrare", re.IGNORECASE)
NOT_RECEIPT_NOTE_RE = re.compile(
    r"scaner|mutare|mutat|corec[tț]i|dubl[aă]|anulat|revert|undo", re.IGNORECASE
)
RECONCILIATION_NOTE_RE = re.compile(r"daily reconciliation", re.IGNORECASE)
# sku_costs placeholder value (80 products carry exactly 10,00): not a real cost.
FILLER_COST = 10.0
# stock-sync went live on 21.07; its ledger is the only record of stock being confirmed.
LEDGER_CUTOVER = "2026-07-21"
# A stock is "counted" once someone physically confirmed it after the cutover — a receipt,
# an inventory count, a scan or a shelf count. Otherwise it is just the figure a store's
# Shopify holds (ROSSI Nails: never counted), and the report keeps
# it out of every total.
PHYSICAL_NOTE_RE = re.compile(
    r"inventar|recep[tț]i|livrare|container|scaner|num[aă]rat|fizic|raft|aliniat",
    re.IGNORECASE,
)
# Moving a pile between warehouses confirms nothing about the quantity.
MOVE_NOTE_RE = re.compile(r"mutare|mutat", re.IGNORECASE)
# A listing waiting in stock-sync's relink review is live on its store but not linked
# to its master yet — a different fix from "listed nowhere".
PENDING_LINK_STATUSES = {"PENDING_RELINK_REVIEW", "NO_MASTER_MATCH"}

# AWB stores that have neither xconnector_domain nor shopify_domain set, matched to
# their stock-sync store by hand (identified from their order-number prefixes).
MANUAL_STORE_DOMAINS: Dict[str, str] = {
    # ORC… orders
    "37c35854-8b4b-4f40-9ccc-79522fc526d6-1786618849-SE8MNAKFPG": "oriceredus.myshopify.com",
    # HU… orders
    "89ac70a9-2ccf-4386-b905-cb858119ae14-1786621119-X2CTQOL3Q6": "63e901-2f.myshopify.com",
    # SK… orders
    "ee9c0806-71fb-4475-a120-43c87e4ba0e1-1786621084-2JVFQ716LH": "16w7xv-0w.myshopify.com",
}


def awb_store_domain(
    uid: str,
    xconnector_domain: Optional[str],
    shopify_domain: Optional[str],
    frisbo_store_slug: Optional[str],
) -> Optional[str]:
    """The Shopify domain stock-sync knows this AWB store by (lower-case)."""
    domain = (
        MANUAL_STORE_DOMAINS.get(uid)
        or xconnector_domain
        or shopify_domain
        or (f"{frisbo_store_slug}.myshopify.com" if frisbo_store_slug else None)
    )
    return domain.strip().lower() if domain else None


def map_awb_to_stock_sync(
    awb_stores: Iterable[dict], stock_sync_stores: Iterable[dict]
) -> Dict[str, str]:
    """AWB store uid → stock-sync store id, matched on the Shopify domain."""
    by_domain = {
        (s.get("shopDomain") or "").strip().lower(): s["id"]
        for s in stock_sync_stores
        if s.get("shopDomain")
    }
    mapping = {}
    for s in awb_stores:
        domain = s.get("domain")
        if domain and domain in by_domain:
            mapping[s["uid"]] = by_domain[domain]
    return mapping


def _safe_div(a: float, b: float) -> Optional[float]:
    return a / b if b else None


def _bucharest_date(utc_naive: datetime):
    return utc_naive.replace(tzinfo=timezone.utc).astimezone(BUCHAREST).date()


def stock_status(
    stock: Optional[float],
    sold: float,
    coverage: Optional[float],
    days_since_last_sale: Optional[int] = None,
    history_days: Optional[int] = None,
    listed: bool = True,
) -> str:
    """Verdict for one row — does this stock move?

    `days_since_last_sale` None means no sale within the last-sale lookback (a year).
    `history_days` is how long the product / store has existed; it can't be dead stock
    ("nothing sold in 90 days") before it has been on sale for 90 days.
    `listed` False = not on any store: it can't sell, and whether it physically exists
    still has to be checked — a different decision from dead stock.
    """
    if stock is None:
        return "fara_date"
    if stock <= 0:
        return "fara_stoc"
    if not sold:
        if not listed:
            return "nelistat"
        # Sales in the period always win: the cached last-sale date can lag them.
        too_new = history_days is not None and history_days < DEAD_DAYS
        if not too_new and (
            days_since_last_sale is None or days_since_last_sale >= DEAD_DAYS
        ):
            return "mort"
        return "nu_se_vinde"
    if coverage is not None and coverage > VERY_SLOW_DAYS:
        return "foarte_lent"
    if coverage is not None and coverage > SLOW_DAYS:
        return "lent"
    if coverage is not None and coverage < SOON_DAYS:
        return "se_termina"
    return "ok"


def physically_counted(ledger: Iterable[dict]) -> set:
    """Masters whose stock was physically confirmed at least once since the cutover."""
    return {
        e["master"]
        for e in ledger
        if e.get("reason") == "RECEIVING"
        or (
            PHYSICAL_NOTE_RE.search(e.get("note") or "")
            and not MOVE_NOTE_RE.search(e.get("note") or "")
        )
    }


def _parse_at(at: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(at.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, AttributeError):
        return None


def stock_arrivals(ledger: Iterable[dict]) -> Dict[str, dict]:
    """master → its last arrival of goods since the cutover:
    {"at": when, "units": net units that arrived, "before": stock it already had}."""
    by_master: Dict[str, list] = defaultdict(list)
    for e in ledger:
        at = _parse_at(e["at"])
        if at:
            by_master[e["master"]].append((at, e))
    undo_window = timedelta(hours=ARRIVAL_UNDO_HOURS)
    out: Dict[str, dict] = {}
    for master, entries in by_master.items():
        entries.sort(key=lambda x: (x[1]["at"], x[1]["created"]))
        for i, (at, e) in enumerate(entries):
            note = e.get("note") or ""
            received = e["after"] - e["before"]
            if (
                e["at"] < LEDGER_SEEDING_END
                or received < MIN_RECEIPT_UNITS
                or RECONCILIATION_NOTE_RE.search(note)
            ):
                continue
            is_receipt = (
                e.get("reason") == "RECEIVING" or RECEIPT_NOTE_RE.search(note)
            ) and not NOT_RECEIPT_NOTE_RE.search(note)
            if not (e["before"] <= 0 or is_receipt):
                continue
            undone = 0.0
            for later_at, later in entries[i + 1 :]:
                if later_at - at > undo_window:
                    break
                if (
                    later.get("reason") == "CORRECTION"
                    and later["after"] < later["before"]
                    and not RECONCILIATION_NOTE_RE.search(later.get("note") or "")
                ):
                    undone += later["before"] - later["after"]
            if received - undone >= MIN_RECEIPT_UNITS:
                out[master] = {
                    "at": at,
                    "units": received - undone,
                    "before": max(e["before"], 0),
                }
    return out


def pick_shopify_costs(variants: Iterable[Tuple[str, float, object]]) -> Dict[str, float]:
    """barcode → cost from (barcode, costPerItem, updated_at) of the RON stores' variants.

    The same product can carry different costs on different stores (set-10-lavete-sarma:
    2,49 on three, 2,14 on one): take the most common, then the most recently updated.
    """
    by_barcode: Dict[str, list] = defaultdict(list)
    for barcode, cost, updated in variants:
        if barcode and cost and cost > 0:
            by_barcode[barcode.strip()].append((round(float(cost), 4), updated))
    out: Dict[str, float] = {}
    for barcode, entries in by_barcode.items():
        counts: Dict[float, int] = defaultdict(int)
        latest: Dict[float, object] = {}
        for cost, updated in entries:
            counts[cost] += 1
            if updated is not None and (cost not in latest or str(updated) > str(latest[cost])):
                latest[cost] = updated
        out[barcode] = max(counts, key=lambda c: (counts[c], str(latest.get(c) or "")))
    return out


def build_rows(
    awb_stores: List[dict],
    snapshot: dict,
    awb_catalog: Iterable[Tuple[str, str, str, str]],
    sales: Dict[Tuple[str, str], dict],
    period_days: int,
    last_sales: Optional[Dict[Tuple[str, str], datetime]] = None,
    costs: Optional[Dict[str, float]] = None,
    now: Optional[datetime] = None,
    first_seen: Optional[Dict[str, datetime]] = None,
    store_first_order: Optional[Dict[str, datetime]] = None,
    arrivals: Optional[Dict[str, dict]] = None,
    counted: Optional[set] = None,
    test_skus: Optional[set] = None,
    store_last_order: Optional[Dict[str, datetime]] = None,
    year_sales: Optional[Dict[Tuple[str, str], float]] = None,
    sale_days: Optional[Dict[Tuple[str, str], set]] = None,
    shopify_costs: Optional[Dict[str, float]] = None,
    vat_rate: float = 0.0,
) -> dict:
    """Build every report row (per store, unallocated, all stores); filtering happens later.

    awb_catalog: (awb_store_uid, sku, barcode, title) for active AWB products — used
        for stores stock-sync doesn't manage, and as a name / SKU fallback.
    sales: (awb_store_uid, sku) → {"units", "net_units", "name"} in the period —
        cancelled, refused and returned parcels excluded; net_units also discounts the
        parcels still on their way by the store's refusal rate (the velocity uses it).
    last_sales: (awb_store_uid, sku) → last non-cancelled order date (naive UTC).
    costs: sku → unit cost in RON from sku_costs — only a fallback.
    shopify_costs: barcode → Shopify costPerItem in RON — the cost used first.
    vat_rate: both costs are WITH VAT — ARONA's COGS formula ends in × 1.21
        (gigi:cogs-audit, sheet "Stoc ARONA" → "COGS 2026") and the profit engine
        divides it back; the stock value is reported without VAT.
    sale_days: (awb_store_uid, sku) → days with a sale, to tell a restock from new goods.
    first_seen: sku → when the product first appeared in the AWB catalog.
    store_first_order: awb store uid → its first order (a new store has no history).
    year_sales: (awb_store_uid, sku) → units over the last year, for the "runs out in"
        estimate of products that sold nothing in the period.
    """
    year_sales = year_sales or {}
    sale_days = sale_days or {}
    shopify_costs = shopify_costs or {}
    first_seen = first_seen or {}
    store_first_order = store_first_order or {}
    arrivals = arrivals or {}
    test_skus = test_skus or set()
    store_last_order = store_last_order or {}
    perfume_store_uids = {
        s["uid"] for s in awb_stores if (s["name"] or "").lower() in PERFUME_STORE_NAMES
    }
    last_sales = last_sales or {}
    costs = costs or {}
    today = _bucharest_date(now or datetime.utcnow())
    awb_to_ss = map_awb_to_stock_sync(awb_stores, snapshot["stores"])
    store_names = {s["uid"]: s["name"] for s in awb_stores}

    stock_by_master: Dict[str, dict] = {
        p["masterProductId"]: p
        for p in snapshot["stock"].values()
        if p.get("masterProductId")
    }
    master_info: Dict[str, dict] = {}
    barcode_to_master: Dict[str, str] = {}
    for m in snapshot.get("masters") or []:
        master_info[m["id"]] = m
        for bc in (m.get("barcodeNormalized"), m.get("barcode")):
            if bc:
                barcode_to_master[bc.strip()] = m["id"]

    listing_master: Dict[Tuple[str, str], str] = {}  # (ss store, sku) → master
    ss_store_masters: Dict[str, Dict[str, set]] = defaultdict(lambda: defaultdict(set))
    master_skus: Dict[str, set] = defaultdict(set)
    master_image: Dict[str, str] = {}
    for listing in snapshot["listings"]:
        master = listing.get("masterProductId")
        if not master:
            continue
        if listing.get("barcodeNormalized"):
            barcode_to_master[listing["barcodeNormalized"]] = master
        sku = (listing.get("sku") or "").strip()
        store_masters = ss_store_masters[listing["storeId"]]
        store_masters[master]  # listed even when the listing has no SKU
        if sku:
            listing_master[(listing["storeId"], sku)] = master
            store_masters[master].add(sku)
            master_skus[master].add(sku)
        if master not in master_image and listing.get("imageUrl"):
            master_image[master] = listing["imageUrl"]

    # Listings not linked to a master say why a product with stock isn't selling.
    ss_store_label = {
        s["id"]: s.get("name") or s.get("shopDomain") or s["id"]
        for s in snapshot["stores"]
    }
    pending_link: Dict[str, set] = defaultdict(set)
    inactive_on: Dict[str, set] = defaultdict(set)
    unlinked_skus: Dict[str, set] = defaultdict(set)  # to name unlinked products
    unlinked_title: Dict[str, str] = {}
    for listing in snapshot.get("other_listings") or []:
        barcode = (listing.get("barcodeNormalized") or listing.get("barcode") or "").strip()
        master = listing.get("masterProductId") or barcode_to_master.get(barcode)
        if not master:
            continue
        if (listing.get("sku") or "").strip():
            unlinked_skus[master].add(listing["sku"].strip())
        title = listing.get("title") or ""
        if title and title != "Default Title" and master not in unlinked_title:
            unlinked_title[master] = title
        store = ss_store_label.get(listing.get("storeId"), listing.get("storeId"))
        if listing.get("matchStatus") in PENDING_LINK_STATUSES:
            pending_link[master].add(store)
        elif listing.get("matchStatus") == "INACTIVE":
            inactive_on[master].add(store)

    catalog_master: Dict[Tuple[str, str], str] = {}  # (awb store, sku) → master
    catalog_name: Dict[str, str] = {}
    catalog_skus_by_master: Dict[str, set] = defaultdict(set)
    for store_uid, sku, barcode, title in awb_catalog:
        sku = (sku or "").strip()
        if not sku:
            continue
        if title and sku not in catalog_name:
            catalog_name[sku] = title
        master = barcode_to_master.get((barcode or "").strip())
        if master:
            catalog_master[(store_uid, sku)] = master
            catalog_skus_by_master[master].add(sku)

    def resolve_master(store_uid: str, sku: str) -> Optional[str]:
        ss_store = awb_to_ss.get(store_uid)
        if ss_store and (ss_store, sku) in listing_master:
            return listing_master[(ss_store, sku)]
        return catalog_master.get((store_uid, sku))

    # Keyed by (awb store, master or "sku:<sku>"); pooled figures keyed by master.
    store_sold: Dict[Tuple[str, str], float] = defaultdict(float)
    store_net: Dict[Tuple[str, str], float] = defaultdict(float)
    pool_net: Dict[str, float] = defaultdict(float)
    master_sale_days: Dict[str, set] = defaultdict(set)
    store_skus: Dict[Tuple[str, str], set] = defaultdict(set)
    store_last: Dict[Tuple[str, str], datetime] = {}
    pool_sold: Dict[str, float] = defaultdict(float)
    pool_last: Dict[str, datetime] = {}
    sale_name: Dict[str, str] = {}
    for (store_uid, sku), agg in sales.items():
        master = resolve_master(store_uid, sku)
        key = (store_uid, master or f"sku:{sku}")
        net = agg.get("net_units", agg["units"])
        store_sold[key] += agg["units"]
        store_net[key] += net
        store_skus[key].add(sku)
        if master:
            pool_sold[master] += agg["units"]
            pool_net[master] += net
        if agg.get("name") and sku not in sale_name:
            sale_name[sku] = agg["name"]
    for (store_uid, sku), days in sale_days.items():
        master = resolve_master(store_uid, sku)
        if master:
            master_sale_days[master] |= days
    store_year: Dict[Tuple[str, str], float] = defaultdict(float)
    pool_year: Dict[str, float] = defaultdict(float)
    for (store_uid, sku), units in year_sales.items():
        master = resolve_master(store_uid, sku)
        store_year[(store_uid, master or f"sku:{sku}")] += units
        if master:
            pool_year[master] += units
    for (store_uid, sku), when in last_sales.items():
        if not when:
            continue
        master = resolve_master(store_uid, sku)
        key = (store_uid, master or f"sku:{sku}")
        if key not in store_last or when > store_last[key]:
            store_last[key] = when
        if master and (master not in pool_last or when > pool_last[master]):
            pool_last[master] = when

    def product_name(master: Optional[str], skus: Iterable[str]) -> str:
        if master:
            for source in (master_info, stock_by_master):
                name = (source.get(master) or {}).get("name") or ""
                # Single-variant Shopify products report their variant title.
                if name and name != "Default Title":
                    return name
        for sku in skus:
            if catalog_name.get(sku) or sale_name.get(sku):
                return catalog_name.get(sku) or sale_name[sku]
        return ""

    # sku_costs is keyed by SKU alone, and the same SKU can be a different product
    # on another store — prefer SKUs that belong to this product only.
    sku_owners: Dict[str, set] = defaultdict(set)
    for (_, sku), master in listing_master.items():
        sku_owners[sku].add(master)
    for (_, sku), master in catalog_master.items():
        sku_owners[sku].add(master)

    def unit_cost(master: Optional[str], skus: Iterable[str]) -> Optional[float]:
        cost = cost_with_vat(master, skus)
        return cost / (1 + vat_rate) if cost else None

    def cost_with_vat(master: Optional[str], skus: Iterable[str]) -> Optional[float]:
        if master:
            for source in (stock_by_master.get(master), master_info.get(master)):
                for bc in ((source or {}).get("barcode"), (source or {}).get("barcodeNormalized")):
                    if bc and shopify_costs.get(bc.strip()):
                        return shopify_costs[bc.strip()]
        candidates = sorted(skus)
        if master:
            candidates += sorted(
                master_skus.get(master, set())
                | catalog_skus_by_master.get(master, set())
            )
            candidates.sort(key=lambda sku: sku_owners.get(sku, {master}) != {master})
        for sku in candidates:
            if costs.get(sku) and costs[sku] != FILLER_COST:
                return costs[sku]
        return None

    # ── Out of scope: perfumes, products still in their test period, placeholders ──
    perfume_ss_stores = {
        awb_to_ss[uid] for uid in perfume_store_uids if uid in awb_to_ss
    }
    excluded_masters = set()
    for ss_store in perfume_ss_stores:
        excluded_masters |= set(ss_store_masters.get(ss_store, {}))
    for (store_uid, _), master in catalog_master.items():
        if store_uid in perfume_store_uids:
            excluded_masters.add(master)
    for master in stock_by_master:
        name = (master_info.get(master) or stock_by_master[master]).get("name") or ""
        if PERFUME_NAME_RE.search(name):
            excluded_masters.add(master)
    for master in set(stock_by_master) | set(master_skus):
        skus = master_skus.get(master, set()) | catalog_skus_by_master.get(
            master, set()
        )
        if skus and skus <= test_skus:
            excluded_masters.add(master)
        if skus and all(PLACEHOLDER_SKU_RE.search(sku) for sku in skus):
            excluded_masters.add(master)

    def excluded_sku(sku: str) -> bool:
        return sku in test_skus or bool(PLACEHOLDER_SKU_RE.search(sku))

    stale_stores = {
        uid
        for uid, last in store_last_order.items()
        if (today - _bucharest_date(last)).days > STALE_STORE_DAYS
    }

    listed_masters = {
        master
        for store_masters in ss_store_masters.values()
        for master in store_masters
    } | set(catalog_master.values())

    def product_age(master: Optional[str], skus: set) -> Optional[int]:
        """Days since the product's FIRST SKU appeared in the catalog — a product
        re-listed under a new SKU on another store is not a new product."""
        candidates = set(skus)
        if master:
            candidates |= master_skus.get(master, set()) | catalog_skus_by_master.get(
                master, set()
            )
        starts = [first_seen[sku] for sku in candidates if sku in first_seen]
        return (today - _bucharest_date(min(starts))).days if starts else None

    def history_days(
        store_uid: str, product_days: Optional[int]
    ) -> Optional[int]:
        """Days the row has had a chance to sell: the younger of product and store."""
        ages = [product_days] if product_days is not None else []
        if store_uid in store_first_order:
            ages.append((today - _bucharest_date(store_first_order[store_uid])).days)
        return min(ages) if ages else None

    def make_row(
        store_uid: str,
        store_name: str,
        master: Optional[str],
        skus: set,
        stock_units: Optional[float],
        sold: float,
        pooled: float,
        last_sale: Optional[datetime],
        year_sold: float = 0.0,
        net_sold: Optional[float] = None,
        stock_is_pool: bool = False,
    ) -> dict:
        stock = stock_by_master.get(master) if master else None
        age = product_age(master, skus)
        history = history_days(store_uid, age)
        listed = master is None or master in listed_masters
        arrival = arrivals.get(master) if master else None
        arrived = arrival["at"] if arrival else None
        # None = no ledger given (tests, or stock-sync ledger unavailable): don't flag.
        uncounted = bool(
            counted is not None
            and master
            and stock_units
            and stock_units > 0
            and master not in counted
        )
        arrived_days = (today - _bucharest_date(arrived)).days if arrived else None
        fresh_goods = False
        old_share = 1.0
        if arrived_days is not None and arrived_days < DEAD_DAYS:
            arrived_on = _bucharest_date(arrived)
            sold_since = any(d >= arrived_on for d in master_sale_days.get(master, ()))
            fresh_goods = arrived_days < NEW_GOODS_GRACE_DAYS or sold_since
            # Old stock = what the product holds beyond the units that arrived (later
            # recounts can add to it); a store row holds the same share of it.
            total = (stock or {}).get("totalUnits") or 0
            old_share = max(0.0, total - arrival["units"]) / total if total > 0 else 0.0
        velocity_basis = None
        # The whole period, unless the product or the store is younger than it, or its
        # goods arrived from zero stock within it (it couldn't sell before).
        velocity_days = (
            period_days
            if history is None
            else max(1, min(period_days, history + 1))
        )
        if arrival and arrival["before"] <= 0 and arrived_days is not None:
            velocity_days = max(1, min(velocity_days, arrived_days + 1))
        if sold:
            velocity = (sold if net_sold is None else net_sold) / velocity_days
            velocity_basis = "perioada"
        elif year_sold:
            # Nothing sold in the period: the last year's pace still says how long the
            # stock lasts, instead of an infinite "never".
            velocity = year_sold / min(YEAR_DAYS, max(history or YEAR_DAYS, period_days))
            velocity_basis = "an"
        else:
            velocity = 0.0
        coverage = _safe_div(stock_units, velocity) if stock_units is not None else None
        # A product not linked to its master yet is still costed by its store SKU.
        cost = unit_cost(
            master, skus or (unlinked_skus.get(master, set()) if master else set())
        )
        days_since = (today - _bucharest_date(last_sale)).days if last_sale else None
        status = stock_status(
            stock_units, sold, coverage if sold else None, days_since, history, listed
        )
        if status in NEGATIVE_STATUSES:
            if store_uid in stale_stores:
                # No orders for a week: sales history isn't reliable, don't judge.
                status = "date_incomplete"
            elif age is not None and age < DEAD_DAYS:
                # A product launched recently: too early to call it dead or slow.
                status = "nou"
            elif fresh_goods:
                # The arrived units are too new to judge; the stock that was already
                # there keeps its verdict.
                old_units = (stock_units or 0) * old_share
                old_status = (
                    stock_status(
                        old_units,
                        sold,
                        _safe_div(old_units, velocity) if sold else None,
                        days_since,
                        history,
                        listed,
                    )
                    if old_units >= MIN_RECEIPT_UNITS
                    else None
                )
                status = old_status if old_status in NEGATIVE_STATUSES else "nou"
        listing_note = None
        if master and not listed:
            if pending_link.get(master):
                if status == "nelistat":
                    status = "legatura_neaprobata"
                listing_note = (
                    f"Pe {', '.join(sorted(pending_link[master]))}: legătura cu "
                    "masterul așteaptă aprobare în stock-sync"
                )
            elif inactive_on.get(master):
                listing_note = f"Inactiv pe {', '.join(sorted(inactive_on[master]))}"
        value = (
            stock_units * cost if (cost and stock_units and stock_units > 0) else None
        )
        surplus_units = (
            max(0, round(stock_units - velocity * SURPLUS_DAYS))
            if stock_units and stock_units > 0
            else None
        )
        surplus_value = surplus_units * cost if (cost and surplus_units) else None
        if cost and surplus_units == 0:
            surplus_value = 0.0
        return {
            "store_uid": store_uid,
            "store_name": store_name,
            "master_product_id": master,
            "sku": ", ".join(
                sorted(skus or (unlinked_skus.get(master, set()) if master else set()))
            ),
            "barcode": stock.get("barcode") if stock else None,
            "product_name": product_name(master, skus)
            or (unlinked_title.get(master, "") if master else ""),
            "image_url": master_image.get(master)
            or (master_info.get(master) or {}).get("imageUrl"),
            "stock": stock_units,
            "stock_is_pool": stock_is_pool,
            "total_units": stock.get("totalUnits") if stock else None,
            "sold_units": round(sold, 2),
            "velocity": round(velocity, 2),
            "velocity_basis": velocity_basis,
            "velocity_days": velocity_days if sold else None,
            "year_sold_units": round(year_sold, 2),
            "coverage_days": round(coverage, 1) if coverage is not None else None,
            "pool_sold_units": round(pooled, 2),
            "last_sale_at": last_sale.isoformat() if last_sale else None,
            "days_since_last_sale": days_since,
            "unit_cost": cost,
            "stock_value": round(value, 2) if value is not None else None,
            "stock_uncounted": uncounted,
            "surplus_units": surplus_units,
            "surplus_value": round(surplus_value, 2)
            if surplus_value is not None
            else None,
            "status": status,
            "arrived_at": arrived.isoformat() if arrived else None,
            "arrived_units": round(arrival["units"]) if arrival else None,
            "days_since_arrival": arrived_days,
            "history_days": history,
            "product_age_days": age,
            "listed": listed,
            "listing_note": listing_note,
            "in_master": bool(stock),
        }

    rows: List[dict] = []
    for store in awb_stores:
        uid, name = store["uid"], store["name"]
        if uid in perfume_store_uids:
            continue
        ss_store = awb_to_ss.get(uid)
        masters: Dict[str, set] = defaultdict(set)
        if ss_store:
            for master, skus in ss_store_masters.get(ss_store, {}).items():
                masters[master] |= skus
        else:
            for (store_uid, sku), master in catalog_master.items():
                if store_uid == uid:
                    masters[master].add(sku)
        for (store_uid, key), skus in store_skus.items():
            if store_uid == uid and not key.startswith("sku:"):
                masters[key] |= skus

        for master, skus in masters.items():
            if master in excluded_masters:
                continue
            stock = stock_by_master.get(master)
            pooled = pool_sold.get(master, 0.0)
            if ss_store:
                units = None
                if stock:
                    units = next(
                        (
                            s.get("currentUnits") or 0
                            for s in stock.get("stores") or []
                            if s.get("storeId") == ss_store
                        ),
                        0,
                    )
                rows.append(
                    make_row(
                        uid,
                        name,
                        master,
                        skus,
                        units,
                        store_sold.get((uid, master), 0.0),
                        pooled,
                        store_last.get((uid, master)),
                        store_year.get((uid, master), 0.0),
                        store_net.get((uid, master), 0.0),
                    )
                )
            else:
                # Not in stock-sync: the store sells from the shared pool, so the
                # pool's stock drains at the pace of every store together.
                row = make_row(
                    uid,
                    name,
                    master,
                    skus,
                    stock.get("totalUnits") if stock else None,
                    pooled,
                    pooled,
                    pool_last.get(master),
                    pool_year.get(master, 0.0),
                    pool_net.get(master, 0.0),
                    stock_is_pool=True,
                )
                row["store_sold_units"] = round(store_sold.get((uid, master), 0.0), 2)
                rows.append(row)

        for (store_uid, key), skus in store_skus.items():
            if (
                store_uid == uid
                and key.startswith("sku:")
                and not any(excluded_sku(sku) for sku in skus)
            ):
                rows.append(
                    make_row(
                        uid,
                        name,
                        None,
                        skus,
                        None,
                        store_sold[(store_uid, key)],
                        0.0,
                        store_last.get((store_uid, key)),
                        store_year.get((store_uid, key), 0.0),
                        store_net.get((store_uid, key), 0.0),
                    )
                )

    for master, stock in stock_by_master.items():
        if master in excluded_masters:
            continue
        total = stock.get("totalUnits") or 0
        pooled = pool_sold.get(master, 0.0)
        skus = set(
            master_skus.get(master) or catalog_skus_by_master.get(master) or set()
        )
        # Produsele ajunse la 0 rămân în listă. Înainte ieșeau din raport (`total > 0 or
        # pooled > 0`), deci nu se puteau nici căuta, nici deschide ca să le vezi mișcările —
        # exact ce-ți trebuie când întrebi „ce s-a întâmplat cu marfa asta". Au status
        # `fara_stoc`, deci nu intră în nicio valoare și stau la coada sortării implicite.
        rows.append(
            make_row(
                ALL_STORES_UID,
                ALL_STORES_NAME,
                master,
                skus,
                total,
                pooled,
                pooled,
                pool_last.get(master),
                pool_year.get(master, 0.0),
                pool_net.get(master, 0.0),
            )
        )
        allocated = sum(
            (s.get("allocatedUnits") or 0) for s in stock.get("stores") or []
        )
        if total - allocated > 0:
            # Unallocated stock has no store of its own — it drains at the pooled rate.
            rows.append(
                make_row(
                    UNALLOCATED_STORE_UID,
                    UNALLOCATED_STORE_NAME,
                    master,
                    skus,
                    total - allocated,
                    pooled,
                    pooled,
                    pool_last.get(master),
                    pool_year.get(master, 0.0),
                    pool_net.get(master, 0.0),
                )
            )

    # On one store the verdict describes that store's share; show how the product
    # does overall next to it, so a store-level "lent" isn't read as a slow product.
    total_status = {
        r["master_product_id"]: r["status"]
        for r in rows
        if r["store_uid"] == ALL_STORES_UID
    }
    for r in rows:
        if r["store_uid"] != ALL_STORES_UID and r["master_product_id"]:
            r["total_status"] = total_status.get(r["master_product_id"])

    last_seen = [
        p.get("lastSeenAt") for p in stock_by_master.values() if p.get("lastSeenAt")
    ]
    return {
        "rows": rows,
        "as_of": max(last_seen) if last_seen else None,
        "stores_without_master": sorted(
            store_names[s["uid"]]
            for s in awb_stores
            if s["uid"] not in awb_to_ss and s["uid"] not in perfume_store_uids
        ),
        "stale_stores": sorted(
            store_names[u] for u in stale_stores if u in store_names
        ),
        "excluded_perfume_stores": sorted(store_names[u] for u in perfume_store_uids),
    }
