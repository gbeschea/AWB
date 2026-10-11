"""Category fallback for Stoc & Viteză. Runs without pytest: `venv/bin/python test_stock_categories.py`."""

from app.api.stock_coverage.categories import categorize


def test_sku_rules_match_the_warehouse():
    assert categorize(["ha-0900-m"], []) == "HA"
    assert categorize(["gd-br-30482"], []) == "Grandia"
    assert categorize(["r203-basecoat"], []) == "Unghii"
    assert categorize(["set-5-lavete-magice"], []) == "Lavete"
    assert categorize(["set-6-crocodil-rosu-27"], []) == "Genți"
    assert categorize(["champagne-5xl"], []) == "Pijamale"
    assert categorize(["geanta-depozitare-crem"], ["casaofertelor.ro"]) == "Deals"


def test_store_rules():
    # Apreciat sells deals too: the deals stores decide, not Apreciat.
    assert categorize(["bradut"], ["apreciat.ro", "casaofertelor.ro"]) == "Deals"
    assert categorize(["albastru"], ["apreciat.ro", "carpetto.ro", "grandia.ro"]) == "Covoare"
    assert categorize(["x"], ["Bonhaus HU"]) == "Deals"
    assert categorize(["x"], ["rossinails.ro", "magdeal.ro"]) == "Unghii"


def test_name_and_code_fallbacks():
    assert categorize(["p01-01"], [], "Polygel Rossi - Soft Pink Glitter") == "Unghii"
    assert categorize(["2300703"], [], "Smoke Leak Detector") == "Grandia"
    assert categorize(["ochelari"], [], "Ochelari de protecție") is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
