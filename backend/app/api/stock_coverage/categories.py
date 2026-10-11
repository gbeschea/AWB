"""
Product category for Stoc & Viteză when the warehouse doesn't have one.

The warehouse (scripts.arona.ro, `api/scan.py` V72-CATEGORII) only labels the SKUs that
sit on a shelf, so everything that lives only in a store's Shopify (ROSSI Nails),
furniture shipped from the supplier (Grandia) or products at 0 stock came back
"Fără categorie". This is the same taxonomy as the warehouse — the SKU pattern first,
then the store the product sells on — so a product reads the same word on both sides.
Keep it in step with `_cat_din_sku` / `_CAT_MAGAZIN` in scan.py.
"""

import re
import unicodedata
from typing import Iterable, Optional

CATEGORIES = [
    "HA", "Grandia", "Parfumuri", "Pijamale", "Genți", "Covoare", "Așternuturi", "Baie",
    "Lavete", "Oglinzi", "Unghii", "Deals",
]

_SIZES = {"xs", "s", "m", "l", "xl", "xxl", "2xl", "3xl", "4xl", "5xl"}
_COLOURS = {
    "negru", "rosu", "roz", "albastru", "alb", "gri", "bej", "verde", "visiniu", "crem",
    "maro", "galben", "mov", "turcoaz", "lila", "champagne",
}
# Store-name fragment → category, by priority: one SKU sells on several stores
# ("baie-albastru" on grandia.ro and ofertelezilei.ro) and the specialised store wins.
# AWB store names aren't all domains ("Bonhaus HU", "Orice Redus"), hence no dots.
_BY_STORE = [
    ("Unghii", ("rossinails",)),
    ("Pijamale", ("nocturna",)),
    # Not apreciat: it sells deals products too (creioane, brad, mănușa); bag SKUs are
    # caught by their code (`genti`, `set-5-`/`set-6-`).
    ("Genți", ("gento",)),
    ("Covoare", ("covoria", "carpetto", "covoareauto")),
    ("Parfumuri", ("esteban", "georgetalent", "nubra", "labnoir", "belasil", "scentum", "duppo")),
    ("Grandia", ("grandia",)),
    ("Deals", (
        "magdeal", "ofertelezilei", "reduceribune", "casaofertelor", "bonhaus",
        "oriceredus", "orice redus", "cepatai", "delabucsa",
    )),
]


def category_from_sku(sku: str) -> Optional[str]:
    s = (sku or "").strip().lower()
    if not s:
        return None
    if re.match(r"^ha[-_ ]?\d", s):
        return "HA"  # before Lavete: `ha-0900-m` is HA, not a size-M cloth
    if s.startswith("gd-"):
        return "Grandia"
    if re.match(r"^r\d{3}", s):
        return "Unghii"  # ROSSI Nails codes: R203-basecoat, R196-top
    if "lavet" in s or re.search(r"\d+-[ms](?:-|$)", s):
        return "Lavete"
    if s.startswith("asternut"):
        return "Așternuturi"
    if s.startswith("oglin"):
        return "Oglinzi"
    if s.startswith("baie") or "covoras-baie" in s:
        return "Baie"
    if s.startswith("covor"):
        return "Covoare"
    if "genti" in s or re.match(r"^set-[56]-", s):
        # not "geanta": `geanta-depozitare-crem` is a deals product; set-5/6 = bag sets
        return "Genți"
    m = re.fullmatch(r"([a-z]+)-([a-z0-9]{1,3})", s)
    if m and m.group(1) in _COLOURS and m.group(2) in _SIZES:
        return "Pijamale"  # `negru-s`, `champagne-5xl` = Nocturna pyjama sets
    return None


def category_from_stores(store_names: Iterable[str]) -> Optional[str]:
    names = " ".join((n or "").lower() for n in store_names)
    for category, keys in _BY_STORE:
        if any(k in names for k in keys):
            return category
    return None


# Last resort for products no store sells any more (stock 0, delisted): the name.
_BY_NAME = [
    ("Unghii", r"polygel|rossi|unghii|oja\b"),
    ("Genți", r"\bgenti\b"),
    ("Pijamale", r"pijama"),
    ("Așternuturi", r"asternut|lenjerie de pat"),
    ("Covoare", r"\bcovor"),
    ("Parfumuri", r"parfum"),
]


def category_from_name(name: str) -> Optional[str]:
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    for category, pattern in _BY_NAME:
        if re.search(pattern, text):
            return category
    return None


def categorize(
    skus: Iterable[str], store_names: Iterable[str], name: str = ""
) -> Optional[str]:
    skus = list(skus)
    for sku in skus:
        category = category_from_sku(sku)
        if category:
            return category
    category = category_from_stores(store_names) or category_from_name(name)
    if category:
        return category
    # A bare 5-7 digit code is a Grandia supplier code (perfumes aren't in this report).
    if any(re.fullmatch(r"\d{5,7}", s or "") for s in skus):
        return "Grandia"
    return None
