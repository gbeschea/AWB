import { useState, useEffect } from 'react'
import { authFetch, API_URL } from '../utils/authFetch'
import { formatNumber } from '../utils/analyticsHelpers'
import { Spinner } from './ui'

const MISCARE = {
    RECEIVING: ['recepție', 'text-emerald-700 dark:text-emerald-400'],
    OPENING_BALANCE: ['sold inițial', 'text-zinc-500 dark:text-zinc-400'],
    CORRECTION: ['corecție', 'text-amber-700 dark:text-amber-400'],
    TRANSFER: ['transfer', 'text-primary-600 dark:text-primary-400'],
    SALE: ['vânzare', 'text-zinc-600 dark:text-zinc-300'],
    RESERVATION: ['rezervare', 'text-zinc-600 dark:text-zinc-300'],
}
const formatMoment = (v) => {
    if (!v) return '—'
    const d = new Date(v)
    if (isNaN(d)) return String(v).slice(0, 16).replace('T', ' ')
    return d.toLocaleString('ro-RO', { day: '2-digit', month: '2-digit', year: '2-digit', hour: '2-digit', minute: '2-digit' })
}

/**
 * Istoricul unui produs, la click pe rând: unde stă pe raft, ce a pățit stocul și ce a luat
 * depozitul de pe raft.
 *
 * Trei surse diferite, spuse ca atare: rafturile vin din aplicația de scanare, mișcările din
 * ledgerul stock-sync (singurul loc unde scrie DE CE s-a schimbat stocul — recepție, inventar,
 * corecție, reconciliere din magazin), ridicările tot din scanare. Dacă o sursă nu răspunde,
 * panoul spune care lipsește: „nicio mișcare" și „n-am putut întreba" nu se confundă.
 */
export default function ProductHistory({ sku, masterId, sections }) {
    // `sections` lasă apelantul să ceară doar o parte: în Depozit, modalul nativ arată deja
    // rafturile și ridicările, dar NU are ledgerul stock-sync (cine a decis și de ce).
    const arata = (nume) => !sections || sections.includes(nume)
    const [data, setData] = useState(null)
    const [error, setError] = useState(null)

    useEffect(() => {
        let cancelled = false
        const q = new URLSearchParams({ sku: sku || '' })
        if (masterId) q.set('master_product_id', masterId)
        authFetch(`${API_URL}/analytics/stock-coverage/product-history?${q}`)
            .then(async (res) => {
                if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`)
                return res.json()
            })
            .then((d) => { if (!cancelled) setData(d) })
            .catch((e) => { if (!cancelled) setError(e.message || 'Eroare la încărcare') })
        return () => { cancelled = true }
    }, [sku, masterId])

    if (error) return <p className="text-xs text-red-600 dark:text-red-400 mt-3">Istoricul nu s-a putut încărca: {error}</p>
    if (!data) return <div className="py-2"><Spinner size="sm" /></div>

    const lipsa = data.unavailable || []
    const H = ({ children }) => <h4 className="text-[11px] uppercase tracking-wider text-zinc-500 dark:text-zinc-400 mb-1.5">{children}</h4>

    return (
        <div className={`mt-4 grid gap-5 ${sections ? '' : 'lg:grid-cols-2'}`}>
            {arata('locations') && (
            <div>
                <H>Unde stă pe raft</H>
                {lipsa.includes('locatii') ? (
                    <p className="text-xs text-amber-700 dark:text-amber-400">Depozitul nu răspunde — locațiile nu se pot arăta acum.</p>
                ) : data.locations.length === 0 ? (
                    <p className="text-xs text-zinc-500 dark:text-zinc-400">Nu e pe niciun raft. (Stocul din raport vine din magazin, nu dintr-o numărătoare.)</p>
                ) : (
                    <table className="w-full text-xs">
                        <thead><tr className="text-zinc-500 dark:text-zinc-400 text-left">
                            <th className="py-1 pr-3 font-medium">Raft</th>
                            <th className="py-1 px-2 font-medium">Stație</th>
                            <th className="py-1 px-2 font-medium text-right">Bucăți</th>
                            <th className="py-1 pl-2 font-medium">Mișcat</th>
                        </tr></thead>
                        <tbody className="divide-y divide-zinc-100 dark:divide-zinc-700">
                            {data.locations.map((l, i) => (
                                <tr key={`${l.shelfCode}-${i}`} className={l.scrap ? 'opacity-60' : ''}>
                                    <td className="py-1 pr-3 text-zinc-800 dark:text-zinc-100">
                                        {l.shelf}
                                        {l.scrap && <span className="ml-1.5 text-[10px] px-1 py-0.5 rounded bg-red-100 text-red-700 dark:bg-red-500/15 dark:text-red-300">rebut</span>}
                                        {l.virtual && !l.scrap && <span className="ml-1.5 text-[10px] px-1 py-0.5 rounded bg-zinc-100 text-zinc-600 dark:bg-zinc-700 dark:text-zinc-300">neașezat</span>}
                                    </td>
                                    <td className="py-1 px-2 text-zinc-600 dark:text-zinc-300 whitespace-nowrap">{l.station}</td>
                                    <td className="py-1 px-2 text-right font-mono text-zinc-900 dark:text-white">{formatNumber(l.qty)}</td>
                                    <td className="py-1 pl-2 text-zinc-500 dark:text-zinc-400 whitespace-nowrap">{formatMoment(l.updatedAt)}</td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                )}

                <H>{`Ridicate din depozit${data.picks.length ? ` (ultimele ${data.picks.length})` : ''}`}</H>
                {lipsa.includes('ridicari') ? (
                    <p className="text-xs text-amber-700 dark:text-amber-400">Depozitul nu răspunde — ridicările nu se pot arăta acum.</p>
                ) : data.picks.length === 0 ? (
                    <p className="text-xs text-zinc-500 dark:text-zinc-400">Nimeni n-a luat nimic de pe raft pentru o comandă.</p>
                ) : (
                    <ul className="text-xs space-y-1">
                        {data.picks.slice(0, 12).map((p, i) => (
                            <li key={`${p.order}-${i}`} className="flex items-center gap-2">
                                <span className="font-mono text-zinc-800 dark:text-zinc-100">{p.order}</span>
                                <span className="text-zinc-500 dark:text-zinc-400">{formatNumber(p.qty)} buc</span>
                                <span className="text-zinc-400 dark:text-zinc-500 truncate">{p.username}</span>
                                <span className="ml-auto text-zinc-500 dark:text-zinc-400 whitespace-nowrap">{formatMoment(p.at)}</span>
                            </li>
                        ))}
                    </ul>
                )}
            </div>
            )}

            {arata('movements') && (
            <div>
                <H>Istoric de mișcări (stoc) · din stock-sync</H>
                {!data.hasMaster ? (
                    <p className="text-xs text-zinc-500 dark:text-zinc-400">Produsul nu e legat de un master în stock-sync, deci nu are istoric de stoc.</p>
                ) : lipsa.includes('miscari') ? (
                    <p className="text-xs text-amber-700 dark:text-amber-400">stock-sync nu răspunde — istoricul nu se poate arăta acum.</p>
                ) : data.movements.length === 0 ? (
                    <p className="text-xs text-zinc-500 dark:text-zinc-400">Nicio mișcare înregistrată.</p>
                ) : (
                    <div className="max-h-72 overflow-y-auto pr-1">
                        <table className="w-full text-xs">
                            <thead className="sticky top-0 bg-white dark:bg-zinc-800"><tr className="text-zinc-500 dark:text-zinc-400 text-left">
                                <th className="py-1 pr-2 font-medium">Când</th>
                                <th className="py-1 px-2 font-medium text-right">Mișcare</th>
                                <th className="py-1 px-2 font-medium text-right">Rămas</th>
                                <th className="py-1 px-2 font-medium">Motiv</th>
                                <th className="py-1 pl-2 font-medium">Cine</th>
                            </tr></thead>
                            <tbody className="divide-y divide-zinc-100 dark:divide-zinc-700">
                                {data.movements.map((m, i) => {
                                    const [eticheta, cls] = MISCARE[m.reason] || [String(m.reason || '—').toLowerCase(), 'text-zinc-600 dark:text-zinc-300']
                                    return (
                                        <tr key={i} title={m.note || ''}>
                                            <td className="py-1 pr-2 text-zinc-600 dark:text-zinc-300 whitespace-nowrap">{formatMoment(m.at)}</td>
                                            <td className={`py-1 px-2 text-right font-mono font-semibold whitespace-nowrap ${m.delta > 0 ? 'text-emerald-600 dark:text-emerald-400' : m.delta < 0 ? 'text-red-600 dark:text-red-400' : 'text-zinc-500'}`}>
                                                {m.delta > 0 ? '+' : ''}{formatNumber(m.delta)}
                                            </td>
                                            <td className="py-1 px-2 text-right font-mono text-zinc-800 dark:text-zinc-100">{formatNumber(m.after)}</td>
                                            <td className="py-1 px-2">
                                                <span className={cls}>{eticheta}</span>
                                                {m.note && <span className="block text-[11px] text-zinc-500 dark:text-zinc-400 truncate max-w-[22rem]">{m.note}</span>}
                                            </td>
                                            <td className="py-1 pl-2 text-zinc-500 dark:text-zinc-400 truncate max-w-[9rem]">{m.actor}</td>
                                        </tr>
                                    )
                                })}
                            </tbody>
                        </table>
                    </div>
                )}
            </div>
            )}
        </div>
    )
}
