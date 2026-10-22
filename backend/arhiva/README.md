# Arhivă — scripturi scoase din uz

Ce e aici NU se rulează. Fișierele păstrează extensia `.ARHIVAT` tocmai ca să nu poată fi pornite din greșeală.

## update_sku_costs.py (scos 22-oct-2026)

Punea costul **10 lei pe TOATE rândurile** din `sku_costs`, dintr-o singură comandă, fără filtru și fără
confirmare. Auditul de COGS din 22-oct-2026 a găsit **497 de rânduri exact la 10,00 lei, fără dată** (20% din
tabelă) — de aici vin. Un cost inventat e mai rău decât unul lipsă: lipsa se vede în rapoarte, 10,00 lei arată
ca un cost adevărat și intră tăcut în profitul fiecărui produs.

Costul mărfii vine acum din `produs_cost` (Order Hub): cost pe LOT, cu moneda și cursul BNR de la recepție,
scris doar prin `POST /api/produs/cost`.
