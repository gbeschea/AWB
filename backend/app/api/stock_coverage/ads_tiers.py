"""
Ads tiers for "Stoc & Viteză": which products to push in ads, which to hold back.

Pure functions, no DB. The endpoint feeds in the report rows and the last 30 days of
ad spend from the metrics warehouse (`cache.product_ad_spend`, Meta + TikTok + Google).

Spend there is keyed by SKU most of the time, but some campaigns are mapped to a
product GROUP ("Creion sprancene", "Lavete abrazive") or only to a brand/account
("Esteban", "UNMAPPED:Bonhaus CZ"). A group is spread over the products whose name
holds every word of the group, in proportion to their 90-day sales; brand/account
spend can't be pinned to a product and is only reported as a total.
"""

import re
import unicodedata
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Tuple

from app.api.stock_coverage.computations import ALL_STORES_UID

# Tier keys, best first. The page keeps the labels; the backend only decides.
TRACTOR = "tractor"  # sells well and the stock lasts: push
MERGE = "merge"  # sells, nothing special: keep as is
SE_TERMINA = "se_termina"  # sells faster than the stock lasts: don't scale, reorder
PREA_MULT = "prea_mult"  # sells, but the stock lasts over 6 months: push harder
NU_SE_VINDE = "nu_se_vinde"  # no sale in 30 days: work on it (ads, promo, bundles)
FARA_STOC = "fara_stoc"  # nothing to sell: no ads
NEJUDECAT = "nejudecat"  # too new, unlisted or incomplete data: no verdict

# A tractor sells at least this many units in 30 days and its stock lasts this long.
TRACTOR_MIN_UNITS_30 = 30
TRACTOR_MIN_COVERAGE_DAYS = 30
# Under this coverage a product that sells is treated as running out (the report's own
# "se_termina" line is 14 days; ads need more notice than reordering does).
RUNNING_OUT_DAYS = 30
SLOW_STATUSES = {"lent", "foarte_lent"}
NOT_SELLING_STATUSES = {"nu_se_vinde", "mort"}
NO_VERDICT_STATUSES = {"nelistat", "legatura_neaprobata", "date_incomplete", "fara_date"}

# Spend keys that name a brand or an ad account, not a product.
_UNMAPPED_RE = re.compile(r"^(unmapped:|test$)", re.IGNORECASE)
# A SKU: "HA-0204", "GD-BR-30482", "set-2-creion-sprancene-gri", "bradut", "2302867".
_SKU_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_STOPWORDS = {"de", "din", "cu", "pentru", "pt", "si", "set", "la", "in"}
STEM_LEN = 5


def ads_tier(row: dict) -> str:
    status = row.get("status")
    sold = row.get("sold_units") or 0
    coverage = row.get("coverage_days")
    if status == "fara_stoc":
        # Still selling at 0 (Grandia pre-orders, oversold HA): it ran out, not "no ads".
        return SE_TERMINA if sold else FARA_STOC
    if status in NO_VERDICT_STATUSES:
        return NEJUDECAT
    if status in NOT_SELLING_STATUSES:
        return NU_SE_VINDE
    if status in SLOW_STATUSES:
        return PREA_MULT
    if status == "nou" and not sold:
        return NEJUDECAT
    if status == "se_termina" or (
        sold and coverage is not None and coverage < RUNNING_OUT_DAYS
    ):
        return SE_TERMINA
    if sold >= TRACTOR_MIN_UNITS_30 and (
        coverage is None or coverage >= TRACTOR_MIN_COVERAGE_DAYS
    ):
        return TRACTOR
    return MERGE


def _plain(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def _stems(text: str) -> List[str]:
    words = re.findall(r"[a-z0-9]+", _plain(text))
    return [w[:STEM_LEN] for w in words if len(w) >= 3 and w not in _STOPWORDS]


def _name_holds(name_words: List[str], stems: List[str]) -> bool:
    return all(any(w.startswith(s) for w in name_words) for s in stems)


def _row_skus(row: dict) -> List[str]:
    return [s.strip().lower() for s in (row.get("sku") or "").split(",") if s.strip()]


def allocate_spend(
    rows: List[dict], spend: Iterable[Tuple[str, str, float]]
) -> Dict[str, object]:
    """Spread the spend over the product rows (one per master product, the all-stores
    view). Returns {"by_master": {master: {"total", "by_platform", "via"}}, "unattributed":
    {key: amount}, "total", "attributed"}."""
    products = [r for r in rows if r.get("master_product_id")]
    by_sku: Dict[str, List[dict]] = defaultdict(list)
    for r in products:
        for sku in _row_skus(r):
            by_sku[sku].append(r)
    name_words = {
        r["master_product_id"]: re.findall(r"[a-z0-9]+", _plain(r.get("product_name") or ""))
        for r in products
    }

    def targets(key: str) -> Tuple[List[dict], str]:
        k = key.strip().lower()
        if k in by_sku:
            return by_sku[k], "sku"
        # Spend on the parent SKU, stock on its variants (HA-1193 → HA-1193-1, -2 …).
        children = [r for s, rs in by_sku.items() if s.startswith(k + "-") for r in rs]
        if children and _SKU_RE.match(k):
            return children, "sku"
        if _SKU_RE.match(k) and " " not in key and any(ch.isdigit() for ch in k):
            return [], ""
        stems = _stems(key)
        if not stems:
            return [], ""
        hits = [r for r in products if _name_holds(name_words[r["master_product_id"]], stems)]
        return hits, f"grup „{key.strip()}”"

    out: Dict[str, dict] = {}
    unattributed: Dict[str, float] = defaultdict(float)
    total = attributed = 0.0
    for key, platform, amount in spend:
        amount = float(amount or 0)
        if amount <= 0 or not key:
            continue
        total += amount
        if _UNMAPPED_RE.match(key.strip()):
            unattributed[key] += amount
            continue
        hits, via = targets(key)
        # De-duplicate (a master can hold several matching SKUs).
        hits = list({r["master_product_id"]: r for r in hits}.values())
        if not hits:
            unattributed[key] += amount
            continue
        weights = [max(r.get("sold_units_90") or 0, 0) for r in hits]
        if not sum(weights):
            weights = [1] * len(hits)
        scale = amount / sum(weights)
        for r, w in zip(hits, weights):
            entry = out.setdefault(
                r["master_product_id"],
                {"total": 0.0, "by_platform": defaultdict(float), "via": set()},
            )
            entry["total"] += w * scale
            entry["by_platform"][platform] += w * scale
            entry["via"].add(via)
        attributed += amount
    return {
        "by_master": {
            m: {
                "total": round(e["total"], 2),
                "by_platform": {p: round(v, 2) for p, v in e["by_platform"].items()},
                "via": sorted(e["via"]),
            }
            for m, e in out.items()
        },
        "unattributed": {k: round(v, 2) for k, v in sorted(unattributed.items(), key=lambda kv: -kv[1])},
        "total": round(total, 2),
        "attributed": round(attributed, 2),
    }


def ads_signal(row: dict) -> Optional[str]:
    """One short line for the mismatch between the tier and what ads do today."""
    tier, spend = row.get("ads_tier"), row.get("ad_spend_30") or 0
    if tier == FARA_STOC and spend > 0:
        return "Reclamă pe produs fără stoc"
    if tier == SE_TERMINA and (row.get("stock") or 0) <= 0:
        return "Vinde fără stoc — precomandă sau supravânzare"
    if tier == SE_TERMINA and spend > 0:
        return "Reclamă pe stoc care se termină"
    if tier == TRACTOR and spend <= 0:
        return "Se vinde bine fără reclamă"
    if tier in (PREA_MULT, NU_SE_VINDE) and spend <= 0:
        return "Stoc greu fără reclamă"
    return None


def annotate(rows: List[dict], spend: Iterable[Tuple[str, str, float]]) -> dict:
    """Adds ads_tier, ad_spend_30, ad_spend_platforms, ad_spend_via, ad_cost_per_unit and
    ads_signal to every row (store rows take their product's spend and tier from the
    all-stores row) and returns the spend summary for the page."""
    totals = [r for r in rows if r.get("store_uid") == ALL_STORES_UID]
    alloc = allocate_spend(totals, spend)
    by_master = alloc["by_master"]
    total_tier = {}
    for r in totals:
        r["ads_tier"] = ads_tier(r)
        total_tier[r.get("master_product_id")] = r["ads_tier"]
    for r in rows:
        m = r.get("master_product_id")
        if r.get("store_uid") != ALL_STORES_UID:
            r["ads_tier"] = total_tier.get(m) or ads_tier(r)
        e = by_master.get(m) if m else None
        r["ad_spend_30"] = e["total"] if e else 0.0
        r["ad_spend_platforms"] = e["by_platform"] if e else {}
        r["ad_spend_via"] = e["via"] if e else []
        # Spend is per product, so cost per unit only reads right against all its sales.
        sold = r.get("sold_units") or 0 if r.get("store_uid") == ALL_STORES_UID else 0
        r["ad_cost_per_unit"] = round(r["ad_spend_30"] / sold, 2) if e and sold else None
        r["ads_signal"] = ads_signal(r)
    return {
        "total": alloc["total"],
        "attributed": alloc["attributed"],
        "unattributed_top": list(alloc["unattributed"].items())[:15],
    }
