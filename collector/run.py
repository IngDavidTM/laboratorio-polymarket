"""Recolector diario de la versión desplegada (GitHub Actions, gratis). Solo datos públicos y dinero virtual.

Qué hace cada día:
  1. Polymarket: mercados con volumen ≥ $100.000 de 10 categorías (mismas búsquedas y límites que la
     fase 3), probabilidad actual del «Sí» y su historial DIARIO (CLOB, fidelity=1440).
  2. Saltos: variación diaria ≥ 10 puntos, agregada por categoría y día con las reglas CONGELADAS de las
     hipótesis F3-PM1..PM5 (registradas el 2026-11-04, ver backend/preregistro/). Cada salto reciente en la
     dirección de la hipótesis se anota como SEÑAL PROSPECTIVA: entrada (virtual) en la apertura de la sesión
     siguiente, salida al cierre tras 5 sesiones, resultado = activo − SPY − costos supuestos.
  3. Calibración prospectiva: mercados vistos ABIERTOS en una ejecución anterior que después se resuelven →
     probabilidad 7 días antes del cierre real frente al resultado (Brier).
  4. Precios de ETF: Yahoo (yfinance) y, si falla, Stooq. Si fallan los dos, se registra el error en
     status.json y NO se resuelve ninguna señal (nunca se inventan datos).

Sin información futura: una señal se anota el día en que el recolector la DETECTA (solo saltos de los
últimos 3 días) y la entrada virtual es la apertura de la primera sesión estrictamente posterior a ese día.
Uso: python run.py [directorio_datos]   (por defecto ../site/data)
"""
from __future__ import annotations

import io
import json
import re
import sys
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
UA = {"User-Agent": "laboratorio-polymarket/1.0 (paper trading research)", "Accept": "application/json"}
MIN_VOLUME = 100_000
MAX_PER_CAT = 150
JUMP = 0.10
HORIZON = 5
START = "2026-11-05"  # inicio de la prueba prospectiva desplegada
SLIPPAGE_BPS = 5.0
SPREAD_TIERS = [(500_000_000, 2), (100_000_000, 4), (25_000_000, 8), (10_000_000, 15), (5_000_000, 25), (0, 50)]

CATEGORIES = {
    "recesion": ["recession", "GDP growth", "negative GDP"],
    "inflacion": ["inflation", "CPI"],
    "politica_monetaria": ["Fed rate cut", "Fed decision", "Fed interest rates", "Powell"],
    "aranceles": ["tariff", "tariffs", "trade deal"],
    "elecciones": ["presidential election", "Senate control", "House control", "election winner"],
    "geopolitica": ["war", "ceasefire", "invasion", "Iran", "Taiwan", "Ukraine", "strike"],
    "regulacion": ["antitrust", "SEC approve", "ban", "Supreme Court"],
    "fiscal": ["government shutdown", "debt ceiling", "budget"],
    "empresas": ["Tesla", "Nvidia", "Apple", "Microsoft", "IPO", "earnings"],
    "energia": ["oil price", "OPEC", "crude"],
}
# Copia literal de backend/app/events/polymarket_adv.py::HYPOTHESES (reglas congeladas).
HYPOTHESES = [
    ("F3-PM1_recesion_TLT", "recesion", r"recession", r"\bno recession\b", "TLT", +1,
     "Si sube la probabilidad de recesión, los bonos largos (TLT) superan a SPY en las sesiones siguientes."),
    ("F3-PM2_recortes_TLT", "politica_monetaria", r"\b(cut|decrease|lower)", r"\bno (cut|change)\b", "TLT", +1,
     "Si sube la probabilidad de recortes de la Fed, TLT supera a SPY."),
    ("F3-PM3_aranceles_GLD", "aranceles", r"tariff", r"\b(no tariff|deal|pause|exempt)", "GLD", +1,
     "Si sube la probabilidad de nuevos aranceles, el oro (GLD) supera a SPY."),
    ("F3-PM4_escalada_GLD", "geopolitica", r"\b(war|invade|invasion|strike|attack)", r"\b(ceasefire|peace|deal)", "GLD", +1,
     "Si sube la probabilidad de escalada militar, GLD supera a SPY."),
    ("F3-PM5_escalada_USO", "geopolitica", r"\b(war|invade|invasion|strike|attack)", r"\b(ceasefire|peace|deal)", "USO", +1,
     "Si sube la probabilidad de escalada militar, el petróleo (USO) supera a SPY."),
]
ASSETS = ["SPY", "TLT", "GLD", "USO"]


def get_json(url: str, params: dict | None = None, tries: int = 3):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    err = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{url[:80]}: {err}")


def _jl(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return []
    return v or []


def load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return default


def save(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=str))


# ---------------------------------------------------------------- Polymarket
def harvest(errors: list) -> dict[str, dict]:
    markets = {}
    for cat, queries in CATEGORIES.items():
        found = {}
        for q in queries:
            for status in ("closed", "active"):
                try:
                    js = get_json(f"{GAMMA}/public-search", {"q": q, "limit_per_type": 50, "events_status": status})
                except Exception as e:  # noqa: BLE001
                    errors.append(f"búsqueda {q}/{status}: {e}")
                    continue
                time.sleep(0.3)
                for ev in js.get("events") or []:
                    for m in ev.get("markets") or []:
                        vol = float(m.get("volumeNum") or m.get("volume") or 0)
                        mid = str(m.get("id"))
                        if vol >= MIN_VOLUME and mid not in markets:
                            found[mid] = (m, ev)
        items = sorted(found.values(), key=lambda x: -float(x[0].get("volumeNum") or 0))[:MAX_PER_CAT]
        for m, ev in items:
            outcomes = _jl(m.get("outcomes"))
            prices = [float(p) for p in _jl(m.get("outcomePrices")) if p not in (None, "")]
            resolved = ""
            if m.get("closed") and prices and max(prices) >= 0.99:
                resolved = outcomes[prices.index(max(prices))] if outcomes else ""
            markets[str(m["id"])] = {
                "id": str(m["id"]), "category": cat, "question": m.get("question", ""),
                "event": ev.get("title", ""), "slug": ev.get("slug", "") or m.get("slug", ""),
                "end_date": m.get("endDate") or ev.get("endDate"), "closed": bool(m.get("closed")),
                "resolved": resolved, "volume": float(m.get("volumeNum") or 0),
                "yes": prices[0] if prices else None, "token": (_jl(m.get("clobTokenIds")) or [None])[0],
                "closed_time": m.get("closedTime"),
            }
    return markets


def history(token: str) -> pd.Series | None:
    js = get_json(f"{CLOB}/prices-history", {"market": token, "interval": "max", "fidelity": 1440})
    h = js.get("history") or []
    if len(h) < 2:
        return None
    s = pd.Series([float(x["p"]) for x in h], index=pd.to_datetime([x["t"] for x in h], unit="s", utc=True))
    return s.groupby(s.index.floor("D")).last()


def jumps(markets: dict, hist: dict, cat: str, yes_pat: str, no_pat: str) -> pd.DataFrame:
    rows = []
    for m in markets.values():
        if m["category"] != cat or m["id"] not in hist:
            continue
        q = m["question"].lower()
        if not re.search(yes_pat, q):
            continue
        orient = -1 if re.search(no_pat, q) else 1
        ch = hist[m["id"]].diff().dropna()
        for ts, v in ch[ch.abs() >= JUMP].items():
            rows.append({"date": ts.tz_convert(None).normalize(), "jump": orient * v, "market": m["id"],
                         "question": m["question"][:140]})
    if not rows:
        return pd.DataFrame(columns=["date", "jump", "n_markets", "questions"])
    ev = pd.DataFrame(rows)
    g = ev.groupby("date").agg(jump=("jump", "sum"), n_markets=("market", "nunique"),
                               questions=("question", lambda x: list(dict.fromkeys(x))[:5]))
    return g[g["jump"].abs() >= JUMP].reset_index()


# ---------------------------------------------------------------- precios
def prices(errors: list) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, str]:
    try:
        import yfinance as yf
        df = yf.download(ASSETS, period="2y", auto_adjust=True, progress=False, threads=False)
        o, c, v = df["Open"][ASSETS], df["Close"][ASSETS], df["Volume"][ASSETS]
        if c.dropna(how="all").empty:
            raise RuntimeError("Yahoo sin filas")
        # la barra del día en curso solo vale tras el cierre (16:00 NY)
        now_ny = pd.Timestamp.now(tz="America/New_York")
        if c.index[-1].date() == now_ny.date() and now_ny.hour < 16:
            o, c, v = o.iloc[:-1], c.iloc[:-1], v.iloc[:-1]
        return o, c, v, "yahoo"
    except Exception as e:  # noqa: BLE001
        errors.append(f"precios Yahoo: {e}")
    try:
        frames = {}
        for a in ASSETS:
            url = f"https://stooq.com/q/d/l/?s={a.lower()}.us&i=d"
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                frames[a] = pd.read_csv(io.StringIO(r.read().decode()), parse_dates=["Date"], index_col="Date")
        o = pd.DataFrame({a: f["Open"] for a, f in frames.items()}).iloc[-600:]
        c = pd.DataFrame({a: f["Close"] for a, f in frames.items()}).iloc[-600:]
        v = pd.DataFrame({a: f["Volume"] for a, f in frames.items()}).iloc[-600:]
        return o, c, v, "stooq"
    except Exception as e:  # noqa: BLE001
        errors.append(f"precios Stooq: {e}")
    return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), "ninguna"


def roundtrip_cost(c: pd.DataFrame, v: pd.DataFrame, asset: str, k: int) -> float:
    """Costo de ida y vuelta supuesto (mismo modelo por tramos que el laboratorio), conocido en k−1."""
    adv = float((c[asset] * v[asset]).iloc[max(0, k - 21):k].mean())
    bps = next(b for a, b in SPREAD_TIERS if adv >= a)
    if float(c[asset].iloc[k - 1]) < 10:
        bps += 10
    return 2 * (bps / 2 + SLIPPAGE_BPS) / 1e4


# ---------------------------------------------------------------- principal
def main(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    now = datetime.now(timezone.utc)
    markets = harvest(errors)
    hist: dict[str, pd.Series] = {}

    def one(m):
        try:
            return m["id"], history(m["token"]), None
        except Exception as e:  # noqa: BLE001
            return m["id"], None, f"historial {m['id']}: {e}"
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(4) as ex:  # 4 peticiones simultáneas: muy por debajo de los límites del CLOB
        for mid, s, err in ex.map(one, [m for m in markets.values() if m["token"]]):
            if err:
                errors.append(err)
            elif s is not None:
                hist[mid] = s

    # 1) foto actual por categoría (solo mercados abiertos)
    snap = {}
    for m in markets.values():
        if m["closed"] or m["yes"] is None:
            continue
        s = hist.get(m["id"])
        ch1 = ch7 = None
        if s is not None and len(s) > 1:
            ch1 = float(s.iloc[-1] - s.iloc[-2])
            if len(s) > 7:
                ch7 = float(s.iloc[-1] - s.iloc[-8])
        spark = [round(float(x), 3) for x in s.iloc[-30:]] if s is not None else []
        snap.setdefault(m["category"], []).append({**{k: m[k] for k in ("id", "question", "event", "slug", "end_date",
                                                                           "volume", "yes")},
                                                   "ch1": ch1, "ch7": ch7, "spark": spark})
    for k in snap:
        snap[k].sort(key=lambda x: -x["volume"])
        snap[k] = snap[k][:40]
    save(data_dir / "polymarket.json", {"updated": now.isoformat(), "categories": snap})

    # 2) calibración prospectiva: SOLO mercados vistos ABIERTOS en una ejecución anterior que después se
    # resuelven. Referencia = el momento real de cierre (closedTime, o hoy si falta) si es anterior a la fecha
    # prevista de fin: muchos mercados se resuelven antes y los precios posteriores ya son 0/1 (información futura).
    cal = load(data_dir / "calibracion.json", {"start": START, "markets": {}, "seen_open": []})
    seen_open = set(cal.get("seen_open", []))
    for m in markets.values():
        if not m["closed"] or not m["resolved"] or m["id"] in cal["markets"] or m["id"] not in seen_open:
            continue
        if m["id"] not in hist:
            continue
        ref = pd.Timestamp(now)
        for t in (m.get("closed_time"), m["end_date"]):
            if t:
                try:
                    x = pd.Timestamp(t)
                    ref = min(ref, x.tz_convert("UTC") if x.tzinfo else x.tz_localize("UTC"))
                except (ValueError, TypeError):
                    pass
        if ref < pd.Timestamp(START, tz="UTC"):  # el periodo del evento terminó antes del despliegue
            continue
        pre = hist[m["id"]][hist[m["id"]].index <= ref - pd.Timedelta(days=7)]
        if pre.empty:
            continue
        cal["markets"][m["id"]] = {"category": m["category"], "question": m["question"], "end": str(ref.date()),
                                   "p7": float(pre.iloc[-1]), "yes": m["resolved"].lower() == "yes"}
    cal["seen_open"] = sorted(seen_open | {m["id"] for m in markets.values() if not m["closed"]})
    summary = {}
    for cat in CATEGORIES:
        rows = [x for x in cal["markets"].values() if x["category"] == cat]
        if rows:
            p = np.array([x["p7"] for x in rows])
            y = np.array([1.0 if x["yes"] else 0.0 for x in rows])
            summary[cat] = {"n": len(rows), "brier": float(((p - y) ** 2).mean()),
                            "brier_base": float(((y.mean() - y) ** 2).mean())}
    cal["summary"] = summary
    save(data_dir / "calibracion.json", cal)

    # 3) precios y señales prospectivas de las hipótesis congeladas
    o, c, v, src = prices(errors)
    sig = load(data_dir / "senales.json", {"start": START, "horizon": HORIZON, "signals": []})
    have = {(s["code"], s["asof"]) for s in sig["signals"]}
    recent_jumps = {}
    today = pd.Timestamp(now.astimezone(timezone.utc).date())
    for code, cat, yes_pat, no_pat, asset, sign, text in HYPOTHESES:
        ev = jumps(markets, hist, cat, yes_pat, no_pat)
        recent_jumps[code] = [{"date": str(r.date.date()), "jump": float(r.jump), "n_markets": int(r.n_markets),
                               "questions": r.questions} for r in ev[ev["date"] >= today - pd.Timedelta(days=30)].itertuples()]
        # solo saltos de los últimos 3 días: uno más antiguo (p. ej. de un mercado que acaba de entrar en la
        # lista) ya no es accionable y anotarlo con su fecha original sería información futura
        for r in ev[(ev["date"] >= today - pd.Timedelta(days=3)) & (ev["date"] >= pd.Timestamp(START))].itertuples():
            asof = str(r.date.date())
            if (r.jump > 0) == (sign > 0) and (code, asof) not in have:
                sig["signals"].append({"code": code, "asset": asset, "asof": asof, "known": str(today.date()),
                                       "jump": float(r.jump), "n_markets": int(r.n_markets),
                                       "questions": r.questions, "logged_at": now.isoformat(), "outcome": None})
                have.add((code, asof))
    if not c.empty:
        sess = c.index
        for s in sig["signals"]:
            if s["outcome"] is not None:
                continue
            # entrada: apertura de la primera sesión POSTERIOR al día en que se detectó (nunca antes)
            k = sess.searchsorted(pd.Timestamp(s["known"]), side="right")
            if k + HORIZON > len(sess):
                continue
            e = k + HORIZON - 1
            a = s["asset"]
            r = float(c[a].iat[e] / o[a].iat[k] - 1)
            rs = float(c["SPY"].iat[e] / o["SPY"].iat[k] - 1)
            cost = roundtrip_cost(c, v, a, k)
            s["outcome"] = {"entry": str(sess[k].date()), "exit": str(sess[e].date()), "ret": r, "spy": rs,
                            "cost": cost, "excess_net": r - cost - rs, "price_source": src}
    res = {}
    for code, *_ in HYPOTHESES:
        x = [s["outcome"]["excess_net"] for s in sig["signals"] if s["code"] == code and s["outcome"]]
        res[code] = {"signals": sum(1 for s in sig["signals"] if s["code"] == code), "resolved": len(x),
                     "mean_excess": float(np.mean(x)) if x else None,
                     "hit_rate": float(np.mean([e > 0 for e in x])) if x else None}
    sig["summary"] = res
    sig["recent_jumps"] = recent_jumps
    sig["hypotheses"] = [{"code": h[0], "category": h[1], "asset": h[4], "text": h[6]} for h in HYPOTHESES]
    save(data_dir / "senales.json", sig)

    # 4) referencia de mercado: retorno de los ETF desde el inicio
    ref = {}
    if not c.empty:
        base = c[c.index >= pd.Timestamp(START)]
        if len(base):
            ref = {a: {"since_start": float(c[a].iat[-1] / base[a].iat[0] - 1),
                       "last": float(c[a].iat[-1]), "date": str(c.index[-1].date())} for a in ASSETS}
    save(data_dir / "status.json", {"updated": now.isoformat(), "markets": len(markets), "with_history": len(hist),
                                    "price_source": src, "assets": ref, "errors": errors[:50],
                                    "n_errors": len(errors)})
    print(f"mercados {len(markets)}, historiales {len(hist)}, precios {src}, errores {len(errors)}")


if __name__ == "__main__":
    try:
        main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "site" / "data")
    except Exception:
        traceback.print_exc()
        sys.exit(1)
