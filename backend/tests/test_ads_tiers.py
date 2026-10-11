"""Ads tiers for Stoc & Viteză. Runs without pytest: `venv/bin/python test_ads_tiers.py`."""

from app.api.stock_coverage.ads_tiers import (
    FARA_STOC,
    MERGE,
    NEJUDECAT,
    NU_SE_VINDE,
    PREA_MULT,
    SE_TERMINA,
    TRACTOR,
    ads_tier,
    allocate_spend,
    annotate,
)
from app.api.stock_coverage.computations import ALL_STORES_UID


def row(master, sku, name="", status="ok", sold=0, sold90=0, coverage=None, stock=100, store=ALL_STORES_UID):
    return {
        "store_uid": store,
        "master_product_id": master,
        "sku": sku,
        "product_name": name,
        "status": status,
        "sold_units": sold,
        "sold_units_90": sold90,
        "coverage_days": coverage,
        "stock": stock,
    }


def test_tiers():
    assert ads_tier(row("m", "a", sold=250, coverage=236, status="nou")) == TRACTOR
    assert ads_tier(row("m", "a", sold=250, coverage=20)) == SE_TERMINA
    assert ads_tier(row("m", "a", sold=10, coverage=90)) == MERGE
    assert ads_tier(row("m", "a", sold=8, coverage=5800, status="foarte_lent")) == PREA_MULT
    assert ads_tier(row("m", "a", status="mort")) == NU_SE_VINDE
    assert ads_tier(row("m", "a", status="nou")) == NEJUDECAT
    # Selling at 0 stock (pre-orders, oversold): it ran out, it isn't "no stock, no ads".
    assert ads_tier(row("m", "a", status="fara_stoc", sold=40, stock=0)) == SE_TERMINA
    assert ads_tier(row("m", "a", status="fara_stoc", stock=0)) == FARA_STOC


def test_spend_by_sku_parent_and_group():
    rows = [
        row("m1", "HA-0204", "Magnet super puternic", sold90=100),
        row("m2", "HA-1193-1, HA-1193-2", "Lampa", sold90=10),
        row("m3", "set-2-creion-sprancene-gri", "Creion Cosmetic pentru Sprâncene Gri", sold90=10),
        row("m4", "set-2-creion-sprancene-maro", "Creion Cosmetic pentru Sprâncene Maro", sold90=30),
        row("m5", "x", "Altceva", sold90=500),
    ]
    out = allocate_spend(
        rows,
        [
            ("HA-0204", "meta", 100.0),
            ("HA-1193", "tiktok", 50.0),
            ("Creion sprancene", "meta", 400.0),
            ("Esteban", "meta", 999.0),
            ("UNMAPPED:Bonhaus CZ", "meta", 1.0),
        ],
    )
    by = out["by_master"]
    assert by["m1"]["total"] == 100.0 and by["m1"]["via"] == ["sku"]
    assert by["m2"]["total"] == 50.0
    # The group is split by 90-day sales: 10 vs 30.
    assert by["m3"]["total"] == 100.0 and by["m4"]["total"] == 300.0
    assert "m5" not in by
    assert out["unattributed"] == {"Esteban": 999.0, "UNMAPPED:Bonhaus CZ": 1.0}
    assert out["attributed"] == 550.0 and out["total"] == 1550.0


def test_annotate_store_rows_take_product_values():
    total = row("m1", "HA-0204", sold=300, coverage=50)
    store = row("m1", "HA-0204", sold=20, coverage=10, store="s1")
    annotate([total, store], [("HA-0204", "meta", 600.0)])
    assert total["ads_tier"] == TRACTOR and store["ads_tier"] == TRACTOR
    assert total["ad_spend_30"] == 600.0 and store["ad_spend_30"] == 600.0
    assert total["ad_cost_per_unit"] == 2.0 and store["ad_cost_per_unit"] is None
    assert total["ads_signal"] is None


def test_signals():
    rows = [
        row("a", "a", status="fara_stoc", stock=0),
        row("b", "b", status="mort"),
        row("c", "c", sold=100, coverage=10),
    ]
    annotate(rows, [("a", "meta", 5.0), ("c", "meta", 5.0)])
    assert rows[0]["ads_signal"] == "Reclamă pe produs fără stoc"
    assert rows[1]["ads_signal"] == "Stoc greu fără reclamă"
    assert rows[2]["ads_signal"] == "Reclamă pe stoc care se termină"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
