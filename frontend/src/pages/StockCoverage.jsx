import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Boxes, ChevronDown, ChevronRight, Download, Layers, Package, PackageSearch, Store as StoreIcon } from 'lucide-react'
import { authFetch, API_URL } from '../utils/authFetch'
import { toastError } from '../utils/toast'
import { formatNumber, formatMoney } from '../utils/analyticsHelpers'
import MultiSelectFilter from '../components/MultiSelectFilter'
import {
    Button,
    ColumnsMenu,
    DataTable,
    EmptyState,
    FilterBar,
    FilterChip,
    FilterDivider,
    KpiCard,
    PageContainer,
    PageHeader,
    PaginationFooter,
    SearchInput,
    Spinner,
} from '../components/ui'
import useColumnVisibility from '../hooks/useColumnVisibility'
import ProductHistory from '../components/ProductHistory'

const PAGE_SIZE = 100
// One report: sales over the last 30 days next to the last 90 (the server merges them).
const CURRENT_DAYS = 30
const ALL_STORES = '__toate__'
const ONE_YEAR = 365

const DEAD_DAYS = 90
const SURPLUS_DAYS = 180

const STATUS = {
    mort: { label: 'Stoc mort', rank: -2, cls: 'bg-red-600 text-white dark:bg-red-500/80 dark:text-white' },
    nelistat: { label: 'Nelistat', rank: -1, cls: 'bg-fuchsia-100 text-fuchsia-700 dark:bg-fuchsia-500/15 dark:text-fuchsia-300' },
    legatura_neaprobata: { label: 'Legătură neaprobată', rank: -0.5, cls: 'bg-violet-100 text-violet-700 dark:bg-violet-500/15 dark:text-violet-300' },
    nu_se_vinde: { label: 'Nu se vinde', rank: 0, cls: 'bg-red-100 text-red-700 dark:bg-red-500/15 dark:text-red-300' },
    foarte_lent: { label: 'Foarte lent', rank: 1, cls: 'bg-orange-100 text-orange-700 dark:bg-orange-500/15 dark:text-orange-300' },
    lent: { label: 'Lent', rank: 2, cls: 'bg-amber-100 text-amber-700 dark:bg-amber-500/15 dark:text-amber-300' },
    ok: { label: 'OK', rank: 3, cls: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-300' },
    nou: { label: 'Marfă intrată recent', rank: 3.5, cls: 'bg-sky-100 text-sky-700 dark:bg-sky-500/15 dark:text-sky-300' },
    se_termina: { label: 'Se termină', rank: 4, cls: 'bg-blue-100 text-blue-700 dark:bg-blue-500/15 dark:text-blue-300' },
    fara_stoc: { label: 'Fără stoc', rank: 5, cls: 'bg-zinc-100 text-zinc-600 dark:bg-zinc-700/60 dark:text-zinc-300' },
    date_incomplete: { label: 'Date incomplete', rank: 5.5, cls: 'bg-zinc-100 text-zinc-600 dark:bg-zinc-700/60 dark:text-zinc-300' },
    fara_date: { label: 'Fără stoc în master', rank: 6, cls: 'bg-zinc-100 text-zinc-500 dark:bg-zinc-700/60 dark:text-zinc-400' },
}

const VIEWS = [
    { key: 'toate', label: 'Toate', match: () => true },
    { key: 'mort', label: `Stoc mort (${DEAD_DAYS}+ zile)`, match: (r) => r.status === 'mort' },
    { key: 'nelistat', label: 'Nelistate / nelegate', match: (r) => r.status === 'nelistat' || r.status === 'legatura_neaprobata' },
    // Dead stock didn't sell in the last 30 days either: it is part of "nu se vând".
    { key: 'nu_se_vinde', label: `Nu se vând (0 în ${CURRENT_DAYS} zile)`, match: (r) => r.status === 'nu_se_vinde' || r.status === 'mort' },
    { key: 'lente', label: 'Lente (peste 6 luni)', match: (r) => r.status === 'lent' || r.status === 'foarte_lent' },
    { key: 'se_vand', label: 'Se vând bine', match: (r) => r.status === 'ok' || r.status === 'se_termina', sort: { key: 'sold_units', direction: 'desc' } },
    { key: 'se_termina', label: 'Se termină', match: (r) => r.status === 'se_termina' },
    { key: 'nou', label: `Marfă intrată recent (sub ${DEAD_DAYS} zile)`, match: (r) => r.status === 'nou' },
    { key: 'nenumarat', label: 'Stoc nenumărat', match: (r) => r.stock_uncounted },
    // Ajunse la zero: nu intră în nicio valoare, dar au istoric — de aici te uiți la mișcări.
    { key: 'fara_stoc', label: 'Ajunse la 0 (doar istoric)', match: (r) => r.status === 'fara_stoc', sort: { key: 'sold_units_90', direction: 'desc' } },
    // Stock without a cost in Costuri SKU is missing from every value — the list to fill in.
    { key: 'fara_cost', label: 'Fără cost', match: (r) => (r.stock || 0) > 0 && r.unit_cost == null, sort: { key: 'stock', direction: 'desc' } },
]

// Valoarea numără TOT stocul, inclusiv ce n-a confirmat nimeni fizic — altfel un magazin
// întreg care trăiește doar în Shopify-ul lui (ROSSI Nails) apărea cu 0 lei, iar totalul pe
// firmă ieșea mai mic decât e. Stocul neconfirmat rămâne marcat pe rând,
// în filtrul „Stoc nenumărat" și pe cardul de valoare — spus, nu ascuns.
const isUncounted = (r) => !!r.stock_uncounted
// Surplusul lasă în continuare afară marfa prea nouă ca să fie judecată.
const countsInSurplus = (r) => r.status !== 'nou'

const DEFAULT_SORT = { key: 'status', direction: 'asc' }

// One row per product holding stock: a pooled store repeats the shared stock on every
// store row, so only its pool row counts.
const productsWithStock = (list) => {
    const byProduct = new Map()
    list.filter((r) => (r.stock || 0) > 0).forEach((r) => {
        const key = r.master_product_id || `${r.store_uid}|${r.sku}`
        if (!byProduct.has(key)) byProduct.set(key, [])
        byProduct.get(key).push(r)
    })
    return [...byProduct.values()].flatMap((group) => {
        const pool = group.find((r) => r.stock_is_pool)
        return pool ? [pool] : group
    })
}

const productKey = (r) => r.master_product_id || `sku:${r.sku}`
const TOTAL_KEY = '__total__'
// Aceeași cheie și la click, și în tabel — altfel rândul pe care dai nu e cel care se deschide.
const rowKeyOf = (r) => (r[TOTAL_KEY] ? TOTAL_KEY : `${r.store_uid}|${r.master_product_id || r.sku}`)

// Case- and diacritics-insensitive, so „covoras” finds „Covoraș”.
const normalizeText = (text) => (text || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase()

// "RON blocați" only counts products with a cost in Costuri SKU — say how many are missing.
const blockedLabel = (value, missing) => {
    const text = `${formatNumber(Math.round(value))} RON blocați`
    return missing ? `${text} · ${formatNumber(missing)} fără cost` : text
}

const formatDateTime = (iso) => {
    if (!iso) return '—'
    return new Date(iso).toLocaleString('ro-RO', {
        timeZone: 'Europe/Bucharest',
        day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit',
    })
}

const formatCoverage = (r) => {
    if (r.stock == null || r.stock <= 0) return '—'
    if (r.coverage_days == null) return r.status === 'nou' ? 'nou, încă nevândut' : 'fără vânzări în 1 an'
    const days = r.coverage_days > ONE_YEAR ? '> 1 an' : `${formatNumber(Math.round(r.coverage_days))} zile`
    // Nothing sold in the period: the estimate uses the last year's pace instead.
    if (r.velocity_basis === 'an') return `${days} (ritm pe 1 an)`
    return r.velocity_basis === '90' ? `${days} (ritm pe 90 zile)` : days
}

const formatSinceSale = (r) => {
    if (r.days_since_last_sale == null) {
        // A store or product younger than the lookback simply hasn't had a sale yet.
        if (r.history_days != null && r.history_days < ONE_YEAR) return `niciodată (în ${formatNumber(r.history_days)} zile)`
        return '> 1 an'
    }
    if (r.days_since_last_sale === 0) return 'azi'
    if (r.days_since_last_sale === 1) return '1 zi'
    return `${formatNumber(r.days_since_last_sale)} zile`
}

const sinceSaleClass = (days) => {
    if (days == null || days >= 90) return 'text-red-600 dark:text-red-400 font-medium'
    if (days >= 30) return 'text-amber-600 dark:text-amber-400'
    return 'text-zinc-600 dark:text-zinc-400'
}

const sortValue = (r, key) => {
    if (key === 'status') return STATUS[r.status]?.rank ?? 9
    if (key === 'days_since_last_sale') return r.days_since_last_sale ?? Number.MAX_SAFE_INTEGER
    if (key === 'coverage_days') {
        if (r.stock == null || r.stock <= 0) return null
        return r.coverage_days ?? Number.MAX_SAFE_INTEGER
    }
    return r[key]
}

// Nulls always sort last; ties on status fall back to the money blocked (highest first).
const compareRows = (a, b, key, dir) => {
    const av = sortValue(a, key)
    const bv = sortValue(b, key)
    if (av == null && bv == null) return 0
    if (av == null) return 1
    if (bv == null) return -1
    const cmp = typeof av === 'number' && typeof bv === 'number'
        ? (av - bv) * dir
        : String(av).localeCompare(String(bv), 'ro') * dir
    if (cmp !== 0 || key !== 'status') return cmp
    return (b.stock_value || 0) - (a.stock_value || 0)
}

function StatusBadge({ status }) {
    const s = STATUS[status] || STATUS.fara_date
    return <span className={`inline-flex px-2 py-0.5 rounded-full text-xs font-medium whitespace-nowrap ${s.cls}`}>{s.label}</span>
}

function StoreBreakdown({ masterId }) {
    const [rows, setRows] = useState(null)
    const [error, setError] = useState(null)

    useEffect(() => {
        let cancelled = false
        authFetch(`${API_URL}/analytics/stock-coverage?master_product_id=${encodeURIComponent(masterId)}`)
            .then(async (res) => {
                if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`)
                return res.json()
            })
            .then((data) => { if (!cancelled) setRows(data.rows || []) })
            .catch((e) => {
                if (cancelled) return
                console.error(e)
                toastError(e)
                setError(e.message || 'Eroare la încărcare')
            })
        return () => { cancelled = true }
    }, [masterId])

    if (error) return <p className="text-xs text-red-600 dark:text-red-400">Nu s-au putut încărca magazinele: {error}</p>
    if (rows == null) return <div className="py-2"><Spinner size="sm" /></div>
    if (rows.length === 0) return <p className="text-xs text-zinc-500 dark:text-zinc-400">Nu e listat pe niciun magazin.</p>
    const sorted = [...rows].sort((a, b) => (b.stock || 0) - (a.stock || 0))
    return (
        <table className="w-full max-w-3xl text-xs">
            <thead>
                <tr className="text-zinc-500 dark:text-zinc-400 text-left">
                    <th className="py-1 pr-3 font-medium">Magazin</th>
                    <th className="py-1 px-2 font-medium">Stare</th>
                    <th className="py-1 px-2 font-medium text-right">Stoc</th>
                    <th className="py-1 px-2 font-medium text-right">Vândute {CURRENT_DAYS} zile</th>
                    <th className="py-1 px-2 font-medium text-right">Vândute {DEAD_DAYS} zile</th>
                    <th className="py-1 px-2 font-medium text-right">Fără vânzare de</th>
                </tr>
            </thead>
            <tbody className="divide-y divide-zinc-100 dark:divide-zinc-700/50">
                {sorted.map((r) => (
                    <tr key={r.store_uid}>
                        <td className="py-1.5 pr-3 text-zinc-800 dark:text-zinc-200">
                            {r.store_name}{r.stock_is_pool && <span className="ml-1 text-zinc-400">(stoc comun)</span>}
                        </td>
                        <td className="py-1.5 px-2"><StatusBadge status={r.status} /></td>
                        <td className="py-1.5 px-2 text-right font-mono text-zinc-800 dark:text-zinc-200">{r.stock == null ? '—' : formatNumber(r.stock)}</td>
                        <td className="py-1.5 px-2 text-right font-mono text-zinc-800 dark:text-zinc-200">{formatNumber(r.stock_is_pool ? r.store_sold_units : r.sold_units)}</td>
                        <td className="py-1.5 px-2 text-right font-mono text-zinc-800 dark:text-zinc-200">{formatNumber(r.sold_units_90)}</td>
                        <td className={`py-1.5 px-2 text-right ${sinceSaleClass(r.days_since_last_sale)}`}>{formatSinceSale(r)}</td>
                    </tr>
                ))}
            </tbody>
        </table>
    )
}

export default function StockCoverage() {
    const [searchParams, setSearchParams] = useSearchParams()
    const view = VIEWS.find((v) => v.key === searchParams.get('arata')) || VIEWS[0]
    const selectedStores = useMemo(
        () => (searchParams.get('magazine') || '').split(',').filter(Boolean),
        [searchParams],
    )
    const isAllStores = selectedStores.length === 0
    const searchQuery = searchParams.get('cauta') || ''
    const selectedProducts = useMemo(
        () => (searchParams.get('produse') || '').split(',').filter(Boolean),
        [searchParams],
    )
    const selectedCategories = useMemo(
        () => (searchParams.get('categorii') || '').split(',').filter(Boolean),
        [searchParams],
    )

    const [report, setReport] = useState(null)
    const [loading, setLoading] = useState(true)
    const [page, setPage] = useState(0)
    const [sort, setSort] = useState(view.sort || DEFAULT_SORT)
    const [expandedKey, setExpandedKey] = useState(null)
    const [exporting, setExporting] = useState(false)
    const [searchText, setSearchText] = useState(searchQuery)
    const requestId = useRef(0)

    const updateParams = (next) => {
        const params = new URLSearchParams(searchParams)
        Object.entries(next).forEach(([k, v]) => (v ? params.set(k, v) : params.delete(k)))
        setSearchParams(params, { replace: true })
        setPage(0)
        setExpandedKey(null)
    }

    const storeParam = isAllStores ? ALL_STORES : selectedStores.join(',')

    const fetchReport = useCallback(async () => {
        const id = ++requestId.current
        setLoading(true)
        try {
            const res = await authFetch(`${API_URL}/analytics/stock-coverage?store_uids=${encodeURIComponent(storeParam)}`)
            if (!res.ok) {
                const err = await res.json().catch(() => ({}))
                throw new Error(err.detail || `HTTP ${res.status}`)
            }
            const data = await res.json()
            if (id === requestId.current) setReport(data)
        } catch (e) {
            if (id === requestId.current) {
                console.error(e)
                toastError(e)
                setReport(null)
            }
        } finally {
            if (id === requestId.current) setLoading(false)
        }
    }, [storeParam])

    useEffect(() => { fetchReport() }, [fetchReport])

    const storeOptions = useMemo(
        () => (report?.stores || [])
            .filter((s) => s.uid !== ALL_STORES)
            .map((s) => ({ value: s.uid, label: s.name })),
        [report],
    )

    // The search narrows everything below it, KPIs included — typing "HA-" gives the
    // HA-only dead stock and money blocked.
    const allRows = useMemo(() => {
        const all = report?.rows || []
        const needle = normalizeText(searchQuery.trim())
        if (!needle) return all
        // 1–2 characters ("ha") would hit names like "Phantom" or "Haine": match SKU prefixes only.
        if (needle.length < 3) {
            return all.filter((r) => (r.sku || '').split(', ').some((sku) => normalizeText(sku).startsWith(needle)))
        }
        return all.filter((r) => normalizeText(`${r.sku} ${r.product_name}`).includes(needle))
    }, [report, searchQuery])

    const productOptions = useMemo(() => {
        const seen = new Map()
        ;(report?.rows || []).forEach((r) => {
            const key = productKey(r)
            if (!seen.has(key)) seen.set(key, { value: key, label: `${r.product_name || '—'} · ${r.sku || '—'}` })
        })
        return [...seen.values()].sort((a, b) => a.label.localeCompare(b.label, 'ro'))
    }, [report])

    // Categories come from the warehouse; without it the filter and summary stay hidden.
    const categoryOptions = useMemo(
        () => (report?.meta?.categories_available ? report.categories || [] : []).map((c) => ({ value: c, label: c })),
        [report],
    )

    const productRows = useMemo(() => {
        if (!selectedProducts.length) return allRows
        const wanted = new Set(selectedProducts)
        return allRows.filter((r) => wanted.has(productKey(r)))
    }, [allRows, selectedProducts])

    const scopedRows = useMemo(() => {
        if (!selectedCategories.length) return productRows
        const wanted = new Set(selectedCategories)
        return productRows.filter((r) => wanted.has(r.category))
    }, [productRows, selectedCategories])

    const rows = useMemo(() => {
        const filtered = scopedRows.filter(view.match)
        if (!sort.key || !sort.direction) return filtered
        const dir = sort.direction === 'asc' ? 1 : -1
        return [...filtered].sort((a, b) => compareRows(a, b, sort.key, dir))
    }, [scopedRows, view, sort])

    const kpis = useMemo(() => {
        const allWithStock = productsWithStock(scopedRows)
        const uncounted = allWithStock.filter(isUncounted)
        const withStock = allWithStock
        const sum = (list) => list.reduce((s, r) => s + (r.stock_value || 0), 0)
        const withSurplus = withStock.filter((r) => countsInSurplus(r) && (r.surplus_units || 0) > 0)
        const newSurplus = withStock.filter((r) => r.status === 'nou' && (r.surplus_units || 0) > 0)
        const noCost = (list) => list.filter((r) => r.stock_value == null).length
        const dead = withStock.filter((r) => r.status === 'mort')
        const unlisted = withStock.filter((r) => r.status === 'nelistat' || r.status === 'legatura_neaprobata')
        const notSelling = withStock.filter((r) => r.status === 'nu_se_vinde' || r.status === 'mort')
        const slow = withStock.filter((r) => r.status === 'lent' || r.status === 'foarte_lent')
        return {
            withStock: withStock.length,
            withStockNoCost: noCost(withStock),
            dead: dead.length,
            deadValue: sum(dead),
            deadNoCost: noCost(dead),
            unlisted: unlisted.length,
            unlistedUnits: unlisted.reduce((s, r) => s + (r.stock || 0), 0),
            unlistedValue: sum(unlisted),
            unlistedNoCost: noCost(unlisted),
            stockValue: sum(withStock),
            notSelling: notSelling.length,
            notSellingValue: sum(notSelling),
            notSellingNoCost: noCost(notSelling),
            notSellingDead: dead.length,
            slow: slow.length,
            slowValue: sum(slow),
            slowNoCost: noCost(slow),
            surplusValue: withSurplus.reduce((s, r) => s + (r.surplus_value || 0), 0),
            surplusUnits: withSurplus.reduce((s, r) => s + r.surplus_units, 0),
            surplusProducts: withSurplus.length,
            surplusNoCost: withSurplus.filter((r) => r.surplus_value == null).length,
            newSurplusValue: newSurplus.reduce((s, r) => s + (r.surplus_value || 0), 0),
            uncounted: uncounted.length,
            uncountedValue: uncounted.reduce((s, r) => s + (r.stock_value || 0), 0),
        }
    }, [scopedRows])

    const categorySummary = useMemo(() => {
        const byCategory = new Map()
        productsWithStock(productRows).forEach((r) => {
            const c = r.category || '—'
            if (!byCategory.has(c)) byCategory.set(c, { category: c, products: 0, value: 0, noCost: 0, notSellingValue: 0, deadValue: 0, surplusValue: 0 })
            const agg = byCategory.get(c)
            agg.products += 1
            agg.value += r.stock_value || 0
            if (r.stock_value == null) agg.noCost += 1
            if (r.status === 'nu_se_vinde' || r.status === 'mort') agg.notSellingValue += r.stock_value || 0
            if (r.status === 'mort') agg.deadValue += r.stock_value || 0
            if (countsInSurplus(r)) agg.surplusValue += r.surplus_value || 0
        })
        return [...byCategory.values()].sort((a, b) => b.value - a.value)
    }, [productRows])

    const soldLabel = `Vândute ${CURRENT_DAYS} zile`
    const showStoreColumn = selectedStores.length > 1

    const columns = [
        {
            key: 'expand', header: '', width: 28, alwaysVisible: true,
            render: (r) => (expandedKey === rowKeyOf(r)
                ? <ChevronDown className="w-4 h-4 text-zinc-400" />
                : <ChevronRight className="w-4 h-4 text-zinc-400" />),
        },
        {
            key: 'store_name', header: 'Magazin', sortable: true, hidden: !showStoreColumn,
            render: (r) => <span className="text-zinc-700 dark:text-zinc-300 whitespace-nowrap">{r.store_name}</span>,
        },
        {
            key: 'product_name', header: 'Produs', sortable: true, alwaysVisible: true,
            render: (r) => (
                <div className="flex items-center gap-2.5 min-w-[260px]">
                    {r.image_url ? (
                        <img src={r.image_url} alt="" loading="lazy" className="w-9 h-9 flex-shrink-0 object-cover rounded-md border border-zinc-200 dark:border-zinc-700" />
                    ) : (
                        <div className="w-9 h-9 flex-shrink-0 rounded-md bg-zinc-100 dark:bg-zinc-800 border border-zinc-200 dark:border-zinc-700 flex items-center justify-center">
                            <Package className="w-4 h-4 text-zinc-400" />
                        </div>
                    )}
                    <div className="min-w-0">
                        <div className="text-zinc-900 dark:text-white leading-snug">{r.product_name || '—'}</div>
                        <div className="text-[11px] font-mono text-zinc-500 dark:text-zinc-400">
                            {r.sku || '—'}
                            {r.stock_is_pool && <span className="ml-1.5 font-sans text-zinc-400">· stoc comun</span>}
                        </div>
                    </div>
                </div>
            ),
        },
        {
            key: 'status', header: 'Stare', sortable: true, alwaysVisible: true,
            render: (r) => (
                <div className="flex flex-col items-start gap-0.5">
                    <StatusBadge status={r.status} />
                    {r.total_status && r.total_status !== r.status && (
                        <span className="text-[11px] text-zinc-500 dark:text-zinc-400 whitespace-nowrap">
                            pe total: {STATUS[r.total_status]?.label || '—'}
                        </span>
                    )}
                    {r.status === 'nou' && (r.arrived_at || r.product_age_days != null) && (
                        <span className="text-[11px] text-zinc-500 dark:text-zinc-400 whitespace-nowrap">
                            {r.arrived_at && r.days_since_arrival < DEAD_DAYS
                                ? `intrat ${formatDateTime(r.arrived_at).split(',')[0]}${r.arrived_units ? ` · +${formatNumber(r.arrived_units)} buc` : ''}`
                                : `produs lansat acum ${formatNumber(r.product_age_days)} zile`}
                        </span>
                    )}
                    {r.stock_uncounted && (
                        <span className="text-[11px] font-medium text-rose-600 dark:text-rose-400 whitespace-nowrap">stoc nenumărat · nu intră în sume</span>
                    )}
                    {r.listing_note && (
                        <span className="text-[11px] text-zinc-500 dark:text-zinc-400 max-w-56">{r.listing_note}</span>
                    )}
                </div>
            ),
        },
        {
            key: 'stock', header: 'Stoc', sortable: true, align: 'right',
            render: (r) => <span className="font-mono">{r.stock == null ? '—' : formatNumber(r.stock)}</span>,
        },
        {
            key: 'category', header: 'Categorie', sortable: true,
            render: (r) => <span className="text-zinc-600 dark:text-zinc-300 whitespace-nowrap">{categoryOptions.length ? r.category : '—'}</span>,
        },
        {
            key: 'sold_units', header: soldLabel, sortable: true, align: 'right',
            render: (r) => (
                <span className="font-mono whitespace-nowrap">
                    {formatNumber(r.sold_units)}
                    {r.stock_is_pool && (
                        <span className="block text-[11px] font-sans text-zinc-500 dark:text-zinc-400">
                            {formatNumber(r.store_sold_units)} pe acest magazin
                        </span>
                    )}
                </span>
            ),
        },
        {
            key: 'sold_units_90', header: `Vândute ${DEAD_DAYS} zile`, sortable: true, align: 'right',
            render: (r) => <span className="font-mono whitespace-nowrap">{formatNumber(r.sold_units_90)}</span>,
        },
        {
            key: 'days_since_last_sale', header: 'Fără vânzare de', sortable: true, align: 'right',
            render: (r) => <span className={`whitespace-nowrap ${sinceSaleClass(r.days_since_last_sale)}`}>{formatSinceSale(r)}</span>,
        },
        {
            key: 'coverage_days', header: 'Se termină în', sortable: true, align: 'right',
            render: (r) => <span className="whitespace-nowrap text-zinc-700 dark:text-zinc-300">{formatCoverage(r)}</span>,
        },
        {
            key: 'stock_value', header: 'Valoare stoc (fără TVA)', sortable: true, align: 'right',
            render: (r) => (
                <span
                    className={`font-mono whitespace-nowrap ${isUncounted(r) ? 'text-zinc-500 dark:text-zinc-400' : ''}`}
                    title={isUncounted(r) ? 'Stoc neconfirmat fizic — cifra vine din Shopify-ul magazinului, nu dintr-o numărătoare. Intră în total.' : undefined}
                >
                    {r.stock_value == null ? '—' : `${formatMoney(r.stock_value)} RON`}{isUncounted(r) ? ' *' : ''}
                </span>
            ),
        },
        {
            key: 'surplus_value', header: 'Rămâne nevândut peste 6 luni', sortable: true, align: 'right',
            render: (r) => (r.surplus_units == null ? <span className="text-zinc-400">—</span> : (
                <div className="flex flex-col items-end">
                    <span className={`font-mono whitespace-nowrap ${!countsInSurplus(r) ? 'text-zinc-400 dark:text-zinc-500' : r.surplus_units > 0 ? 'text-orange-600 dark:text-orange-400' : 'text-zinc-500 dark:text-zinc-400'}`}>
                        {r.surplus_value == null ? 'fără cost' : `${formatMoney(r.surplus_value)} RON`}
                    </span>
                    <span className="text-[11px] text-zinc-500 dark:text-zinc-400 whitespace-nowrap">{formatNumber(r.surplus_units)} buc</span>
                </div>
            )),
        },
        {
            key: 'velocity', header: 'Buc/zi', sortable: true, align: 'right',
            render: (r) => <span className="font-mono">{r.velocity.toLocaleString('ro-RO', { maximumFractionDigits: 2 })}</span>,
        },
        {
            key: 'pool_sold_units', header: 'Vândute pe toate magazinele', sortable: true, align: 'right',
            render: (r) => <span className="font-mono">{formatNumber(r.pool_sold_units)}</span>,
        },
        {
            key: 'total_units', header: 'Stoc total depozit', sortable: true, align: 'right',
            render: (r) => <span className="font-mono">{r.total_units == null ? '—' : formatNumber(r.total_units)}</span>,
        },
    ]

    const defaultVisible = ['expand', 'store_name', 'product_name', 'category', 'status', 'stock', 'sold_units', 'sold_units_90', 'days_since_last_sale', 'coverage_days', 'stock_value', 'surplus_value']
    const { visibleKeys, setVisibleKeys, defaultVisibleKeys } =
        useColumnVisibility('stock-coverage-v4', columns, defaultVisible)
    const tableColumns = columns.filter((c) => !c.hidden)

    const totals = useMemo(() => {
        const sum = (key, keep = () => true) => rows.filter(keep).reduce((s, r) => s + (r[key] || 0), 0)
        return {
            count: rows.length,
            withStock: rows.filter((r) => (r.stock || 0) > 0).length,
            uncounted: rows.filter(isUncounted).length,
            stock: sum('stock'),
            sold_units: sum('sold_units'),
            sold_units_90: sum('sold_units_90'),
            stock_value: sum('stock_value'),
            surplus_value: sum('surplus_value', countsInSurplus),
            surplus_units: sum('surplus_units', countsInSurplus),
            velocity: sum('velocity'),
            pool_sold_units: sum('pool_sold_units'),
        }
    }, [rows])

    const TOTAL_CELLS = {
        product_name: () => <span className="font-semibold text-zinc-900 dark:text-white">TOTAL · {formatNumber(totals.count)} produse ({formatNumber(totals.withStock)} cu stoc{totals.uncounted ? `, din care ${formatNumber(totals.uncounted)} nenumărat fizic` : ''})</span>,
        stock: () => <span className="font-mono font-semibold">{formatNumber(totals.stock)}</span>,
        sold_units: () => <span className="font-mono font-semibold">{formatNumber(totals.sold_units)}</span>,
        sold_units_90: () => <span className="font-mono font-semibold">{formatNumber(totals.sold_units_90)}</span>,
        stock_value: () => <span className="font-mono font-semibold whitespace-nowrap">{formatMoney(totals.stock_value)} RON</span>,
        surplus_value: () => (
            <div className="flex flex-col items-end">
                <span className="font-mono font-semibold whitespace-nowrap">{formatMoney(totals.surplus_value)} RON</span>
                <span className="text-[11px] text-zinc-500 dark:text-zinc-400 whitespace-nowrap">{formatNumber(totals.surplus_units)} buc</span>
            </div>
        ),
        velocity: () => <span className="font-mono font-semibold">{totals.velocity.toLocaleString('ro-RO', { maximumFractionDigits: 2 })}</span>,
        pool_sold_units: () => <span className="font-mono font-semibold">{formatNumber(totals.pool_sold_units)}</span>,
    }
    const columnsWithTotal = tableColumns.map((c) => ({
        ...c,
        sortable: c.sortable,
        render: (r) => (r[TOTAL_KEY] ? (TOTAL_CELLS[c.key]?.() ?? null) : c.render(r)),
    }))

    const pageRows = rows.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE)
    const tableRows = rows.length ? [...pageRows, { [TOTAL_KEY]: true }] : pageRows

    const handleStoresChange = (uids) => updateParams({ magazine: uids.join(',') })
    const handleCategoriesChange = (values) => updateParams({ categorii: values.join(',') })
    const handleCategoryRowClick = (category) => updateParams({
        categorii: selectedCategories.length === 1 && selectedCategories[0] === category ? '' : category,
    })
    const handleViewChange = (key) => {
        // "Se vând bine" reads best sorted by units sold; the other views by verdict.
        setSort(VIEWS.find((v) => v.key === key)?.sort || DEFAULT_SORT)
        updateParams({ arata: key === 'toate' ? '' : key })
    }
    const handleSearchCommit = (text) => updateParams({ cauta: text.trim() })
    const handleSortChange = (next) => {
        setSort(next)
        setPage(0)
    }
    const handleProductsChange = (keys) => updateParams({ produse: keys.join(',') })
    const handleRowClick = (r) => {
        if (r[TOTAL_KEY]) return
        const key = rowKeyOf(r)
        setExpandedKey((cur) => (cur === key ? null : key))
    }
    // Pe „Toate magazinele" se vede întâi defalcarea pe magazine; în rest, direct istoricul.
    const renderRowDetails = (r) => (
        <>
            {isAllStores && r.master_product_id && <StoreBreakdown masterId={r.master_product_id} />}
            <ProductHistory sku={r.sku} masterId={r.master_product_id} />
        </>
    )

    const handleExportExcel = async () => {
        setExporting(true)
        try {
            const XLSX = await import('xlsx').then((m) => m.default || m)
            const data = rows.map((r) => ({
                'Magazin': r.store_name,
                'SKU': r.sku,
                'Produs': r.product_name,
                'Stare': STATUS[r.status]?.label || '',
                'Stoc': r.stock ?? '',
                'Stoc comun (toate magazinele)': r.stock_is_pool ? 'da' : '',
                Categorie: r.category || '',
                [soldLabel]: r.stock_is_pool ? r.store_sold_units : r.sold_units,
                [`Vândute ${DEAD_DAYS} zile`]: r.sold_units_90,
                'Fără vânzare de (zile)': r.days_since_last_sale ?? '> 365',
                'Se termină în (zile)': r.coverage_days ?? formatCoverage(r),
                'Cost unitar fără TVA (RON)': r.unit_cost ?? '',
                'Valoare stoc fără TVA (RON)': r.stock_value ?? '',
                'Stoc nenumărat (nu intră în sume)': r.stock_uncounted ? 'da' : '',
                'Rămâne nevândut peste 6 luni (buc)': r.surplus_units ?? '',
                'Rămâne nevândut peste 6 luni fără TVA (RON)': r.surplus_value ?? '',
                'Stare pe total': r.total_status ? (STATUS[r.total_status]?.label || '') : '',
                'Buc/zi': r.velocity,
                'Vândute pe toate magazinele': r.pool_sold_units,
                'Stoc total depozit': r.total_units ?? '',
                'Cod de bare': r.barcode ?? '',
            }))
            const ws = XLSX.utils.json_to_sheet(data)
            ws['!cols'] = Object.keys(data[0] || {}).map((k) => ({ wch: k === 'Produs' ? 60 : Math.max(k.length + 2, 12) }))
            const wb = XLSX.utils.book_new()
            XLSX.utils.book_append_sheet(wb, ws, 'Stoc & Viteza')
            XLSX.writeFile(wb, `stoc-viteza-${view.key}-${new Date().toISOString().slice(0, 10)}.xlsx`)
        } catch (e) {
            console.error(e)
            toastError(e)
        } finally {
            setExporting(false)
        }
    }

    const meta = report?.meta

    return (
        <PageContainer>
            <PageHeader
                title="Stoc & Viteză"
                subtitle="Ce nu se vinde: stocul din master, cât s-a vândut și de când n-a mai ieșit nimic"
                icon={Boxes}
                actions={
                    <Button
                        variant="secondary"
                        icon={Download}
                        onClick={handleExportExcel}
                        loading={exporting}
                        disabled={loading || rows.length === 0}
                        data-action="export-stock-coverage"
                    >
                        Export Excel
                    </Button>
                }
            />

            <FilterBar>
                <MultiSelectFilter
                    label="Magazine"
                    options={storeOptions}
                    selected={selectedStores}
                    onChange={handleStoresChange}
                    icon={StoreIcon}
                    searchable
                    allLabel="Toate magazinele"
                />
                {categoryOptions.length > 0 && (
                    <MultiSelectFilter
                        label="Categorii"
                        options={categoryOptions}
                        selected={selectedCategories}
                        onChange={handleCategoriesChange}
                        icon={Layers}
                        allLabel="Toate categoriile"
                    />
                )}
                <MultiSelectFilter
                    label="Produse"
                    options={productOptions}
                    selected={selectedProducts}
                    onChange={handleProductsChange}
                    icon={PackageSearch}
                    searchable
                    allLabel="Toate produsele"
                />
                <SearchInput
                    value={searchText}
                    onChange={setSearchText}
                    onDebouncedChange={handleSearchCommit}
                    debounce={300}
                    placeholder="Caută SKU sau produs (ex: HA-)"
                    className="min-w-[220px]"
                />
                <FilterDivider />
                {VIEWS.map((v) => (
                    <FilterChip
                        key={v.key}
                        active={view.key === v.key}
                        onClick={() => handleViewChange(v.key)}
                        label={v.label}
                    />
                ))}
                <div className="ml-auto flex items-center gap-3 text-xs text-zinc-500 dark:text-zinc-400">
                    <span>Stoc la: <span className="font-medium text-zinc-700 dark:text-zinc-300">{formatDateTime(meta?.stock_as_of)}</span></span>
                    {meta?.shopify_costs_as_of && (
                        <span>Costuri la: <span className="font-medium text-zinc-700 dark:text-zinc-300">{formatDateTime(meta.shopify_costs_as_of)}</span></span>
                    )}
                    {categoryOptions.length > 0 && meta?.categories_as_of && (
                        <span>Categorii la: <span className="font-medium text-zinc-700 dark:text-zinc-300">{formatDateTime(meta.categories_as_of)}</span></span>
                    )}
                    <ColumnsMenu
                        columns={tableColumns}
                        visibleKeys={visibleKeys}
                        onChange={setVisibleKeys}
                        defaultVisibleKeys={defaultVisibleKeys}
                    />
                </div>
            </FilterBar>

            <div className="grid grid-cols-2 lg:grid-cols-3 2xl:grid-cols-6 gap-3">
                <KpiCard
                    label="Valoare stoc — cât ne-a costat marfa (fără TVA)"
                    value={`${formatNumber(Math.round(kpis.stockValue))} RON`}
                    trendLabel={`stoc × costul din Shopify ÷ 1,21, pe ${formatNumber(kpis.withStock)} produse${kpis.withStockNoCost ? ` · ${formatNumber(kpis.withStockNoCost)} fără cost, neincluse` : ''}${kpis.uncounted ? ` · din care ${formatNumber(Math.round(kpis.uncountedValue))} RON pe ${formatNumber(kpis.uncounted)} produse cu stoc neconfirmat fizic` : ''}`}
                    color="blue"
                />
                <KpiCard
                    label={`Stoc mort (nimic vândut în ${DEAD_DAYS} zile)`}
                    value={formatNumber(kpis.dead)}
                    trendLabel={blockedLabel(kpis.deadValue, kpis.deadNoCost)}
                    color="red"
                />
                <KpiCard
                    label="Nelistate / nelegate de master"
                    value={formatNumber(kpis.unlisted)}
                    trendLabel={`${formatNumber(kpis.unlistedUnits)} buc · ${blockedLabel(kpis.unlistedValue, kpis.unlistedNoCost)}`}
                    color="violet"
                />
                <KpiCard
                    label={`Nu s-au vândut în ultimele ${CURRENT_DAYS} de zile`}
                    value={formatNumber(kpis.notSelling)}
                    trendLabel={`${blockedLabel(kpis.notSellingValue, kpis.notSellingNoCost)} · din care ${formatNumber(kpis.notSellingDead)} stoc mort (nimic nici în ${DEAD_DAYS} zile)`}
                    color="amber"
                />
                <KpiCard
                    label="Lente (stoc peste 6 luni)"
                    value={formatNumber(kpis.slow)}
                    trendLabel={blockedLabel(kpis.slowValue, kpis.slowNoCost)}
                    color="zinc"
                />
                <KpiCard
                    label={`Rămâne nevândut peste ${SURPLUS_DAYS / 30} luni`}
                    value={`${formatNumber(Math.round(kpis.surplusValue))} RON`}
                    trendLabel={`${formatNumber(kpis.surplusUnits)} buc din ${formatNumber(kpis.surplusProducts)} produse${kpis.surplusNoCost ? ` · ${formatNumber(kpis.surplusNoCost)} fără cost` : ''}${kpis.newSurplusValue ? ` · + ${formatNumber(Math.round(kpis.newSurplusValue))} RON marfă intrată recent, nejudecată` : ''}`}
                    color="orange"
                />
            </div>

            {categoryOptions.length > 0 && categorySummary.length > 1 && (
                <div className="overflow-x-auto rounded-xl border border-zinc-200 dark:border-zinc-700 bg-white dark:bg-zinc-800">
                    <table className="w-full text-sm">
                        <thead className="bg-zinc-50 dark:bg-zinc-900 text-xs uppercase tracking-wider text-zinc-500 dark:text-zinc-300">
                            <tr>
                                <th className="px-3 py-2 text-left font-medium">Categorie</th>
                                <th className="px-3 py-2 text-right font-medium">Produse cu stoc</th>
                                <th className="px-3 py-2 text-right font-medium">Valoare stoc (fără TVA)</th>
                                <th className="px-3 py-2 text-right font-medium">Nu s-au vândut în {CURRENT_DAYS} zile</th>
                                <th className="px-3 py-2 text-right font-medium">din care stoc mort</th>
                                <th className="px-3 py-2 text-right font-medium">Rămâne nevândut peste 6 luni</th>
                            </tr>
                        </thead>
                        <tbody className="divide-y divide-zinc-100 dark:divide-zinc-700">
                            {categorySummary.map((c) => (
                                <tr
                                    key={c.category}
                                    onClick={() => handleCategoryRowClick(c.category)}
                                    data-action="filter-category"
                                    className={`cursor-pointer hover:bg-zinc-50 dark:hover:bg-zinc-700/30 ${selectedCategories.includes(c.category) ? 'bg-blue-50 dark:bg-blue-950/40' : ''}`}
                                >
                                    <td className="px-3 py-1.5 font-medium text-zinc-900 dark:text-white">{c.category}</td>
                                    <td className="px-3 py-1.5 text-right font-mono text-zinc-700 dark:text-zinc-300">
                                        {formatNumber(c.products)}{c.noCost > 0 && <span className="ml-1 text-[11px] font-sans text-zinc-500 dark:text-zinc-400">({formatNumber(c.noCost)} fără cost)</span>}
                                    </td>
                                    <td className="px-3 py-1.5 text-right font-mono font-semibold text-zinc-900 dark:text-white">{formatMoney(c.value)} RON</td>
                                    <td className="px-3 py-1.5 text-right font-mono text-amber-700 dark:text-amber-300">{formatMoney(c.notSellingValue)} RON</td>
                                    <td className="px-3 py-1.5 text-right font-mono text-red-600 dark:text-red-400">{formatMoney(c.deadValue)} RON</td>
                                    <td className="px-3 py-1.5 text-right font-mono text-orange-600 dark:text-orange-400">{formatMoney(c.surplusValue)} RON</td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            )}

            <DataTable
                columns={columnsWithTotal}
                rows={tableRows}
                rowKey={rowKeyOf}
                rowClassName={(r) => (r[TOTAL_KEY] ? '*:sticky *:bottom-0 *:z-10 *:bg-zinc-100 dark:*:bg-zinc-900 *:border-t-2 *:border-zinc-300 dark:*:border-zinc-600' : '')}
                loading={loading}
                visibleColumnKeys={visibleKeys}
                sort={sort}
                onSort={handleSortChange}
                onRowClick={handleRowClick}
                expandedKey={expandedKey}
                renderExpanded={renderRowDetails}
                maxHeight="max(480px, calc(100vh - 140px))"
                empty={
                    <EmptyState
                        icon={Boxes}
                        title={report ? (searchQuery ? `Niciun produs pentru „${searchQuery}”` : 'Niciun produs pentru filtrele alese') : 'Raportul nu s-a putut încărca'}
                        description={report ? 'Alege alt magazin, altă categorie sau „Toate”.' : 'Verifică conexiunea la stock-sync și încearcă din nou.'}
                        action={!report && <Button variant="primary" onClick={fetchReport} data-action="retry-stock-coverage">Reîncearcă</Button>}
                    />
                }
                footer={
                    <PaginationFooter
                        page={page}
                        pageSize={PAGE_SIZE}
                        total={rows.length}
                        onPageChange={setPage}
                        loading={loading}
                    />
                }
            />
        </PageContainer>
    )
}
