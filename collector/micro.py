"""Microestructura de Polymarket en vivo (fase 4): operaciones EN PAPEL a precios reales del libro.

Nada de dinero real: solo se anota qué se habría comprado al mejor precio de venta visible (ask), con la
comisión real del mercado, y se resuelve cuando el mercado cierra. Sirve para comprobar fuera de muestra lo
que el estudio histórico (precios diarios y diferencial supuesto) solo puede aproximar.

  · diferenciales reales por tramo de volumen (contrasta el costo supuesto en el histórico)
  · arbitraje de suma en eventos de resultados excluyentes, con asks reales y profundidad
  · reglas registradas F4 (parámetros copiados de backend/app/research/pm_micro.py; no se tocan aquí)

Fuentes: Gamma /markets/keyset y /events/{id} (mejor compra/venta), CLOB /books (profundidad). Sin clave.
"""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
MICRO_START = "2026-11-10"
MAX_PAGES = 60          # 6.000 mercados abiertos como mucho
MAX_EVENTS = 300        # eventos negRisk revisados por ejecución
ARB_MARGIN = 0.02       # F4-A1: ganancia neta mínima
MIN_DEPTH_USD = 20.0    # profundidad mínima al mejor precio para anotar una operación
# Reglas congeladas (ver preregistro F4-*.json). `None` = regla descartada en desarrollo: no se opera.
# F4-C1 (favoritos) se rechazó en desarrollo y F4-N1 (comprar NO) en la prueba sellada el 2026-11-10: no se operan.
RULES: dict[str, dict | None] = {"F4-C1": None, "F4-N1": None}


def _jl(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:  # noqa: BLE001
            return None
    return v


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _ts(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00").replace(" ", "T").replace("+00", "+00:00").replace("+00:00:00", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _market(m: dict, strict: bool = True) -> dict | None:
    outs, toks = _jl(m.get("outcomes")), _jl(m.get("clobTokenIds"))
    bid, ask = _f(m.get("bestBid")), _f(m.get("bestAsk"))
    if not outs or not toks or len(outs) != 2:
        return None
    if strict and (bid is None or ask is None or not (0 < bid < ask < 1)):
        return None
    fs = m.get("feeSchedule") if isinstance(m.get("feeSchedule"), dict) else {}
    ev = (m.get("events") or [{}])[0]
    return {"id": str(m["id"]), "question": m.get("question", ""), "slug": ev.get("slug") or m.get("slug", ""),
            "event_id": str(ev.get("id") or ""), "event": ev.get("title", ""), "out0": outs[0], "out1": outs[1],
            "tok0": toks[0], "tok1": toks[1], "bid": bid, "ask": ask, "volume": _f(m.get("volumeNum")) or 0.0,
            "end": m.get("endDate"), "start": m.get("startDate") or m.get("createdAt"),
            "neg_risk": bool(m.get("negRisk")), "fee_rate": _f(fs.get("rate")) or 0.0 if m.get("feesEnabled") else 0.0,
            "accepting": bool(m.get("acceptingOrders", True))}


def universe(get_json, errors: list) -> list[dict]:
    out, cur = [], None
    for _ in range(MAX_PAGES):
        try:
            r = get_json(f"{GAMMA}/markets/keyset", {"closed": "false", "limit": 100, "volume_num_min": 10000,
                                                    "order": "volumeNum", "ascending": "false",
                                                    **({"after_cursor": cur} if cur else {})})
        except Exception as e:  # noqa: BLE001
            errors.append(f"micro universo: {e}")
            break
        for m in r.get("markets") or []:
            x = _market(m)
            if x and x["accepting"]:
                out.append(x)
        cur = r.get("next_cursor")
        if not cur or not r.get("markets"):
            break
    return out


def books(tokens: list[str], errors: list) -> dict[str, dict]:
    """Mejor venta (ask) y dólares disponibles a ese precio, por ficha."""
    out = {}
    for i in range(0, len(tokens), 100):
        try:
            req = urllib.request.Request(f"{CLOB}/books", data=json.dumps([{"token_id": t} for t in tokens[i:i + 100]]).encode(),
                                         headers={"Content-Type": "application/json", "User-Agent": "laboratorio-polymarket/1.0"})
            with urllib.request.urlopen(req, timeout=40) as r:
                for b in json.load(r):
                    asks = sorted(((float(a["price"]), float(a["size"])) for a in b.get("asks") or []))
                    if asks:
                        out[b["asset_id"]] = {"ask": asks[0][0], "usd": asks[0][0] * asks[0][1]}
        except Exception as e:  # noqa: BLE001
            errors.append(f"micro libros: {e}")
    return out


def fee(rate: float, p: float) -> float:
    return rate * p * (1 - p)


def _resolve(get_json, trades: list[dict], errors: list) -> None:
    """Cierra operaciones en papel cuyo mercado ya se resolvió (pago = precio final de la ficha comprada)."""
    for t in trades:
        if t.get("outcome") is not None:
            continue
        try:
            pays = []
            for leg in t["legs"]:
                m = get_json(f"{GAMMA}/markets/{leg['id']}")
                prices = _jl(m.get("outcomePrices"))
                if not m.get("closed") or not prices:
                    pays = None
                    break
                pays.append(float(prices[leg["side"]]))
            if pays is None:
                continue
            pay = sum(pays)
            t["outcome"] = {"payoff": pay, "ret": (pay - t["cost"]) / t["cost"], "resolved_at": datetime.now(timezone.utc).isoformat()}
        except Exception as e:  # noqa: BLE001
            errors.append(f"micro resolver {t.get('key')}: {e}")


def _summary(trades: list[dict]) -> dict:
    out = {}
    for t in trades:
        s = out.setdefault(t["code"], {"trades": 0, "resolved": 0, "sum": 0.0, "wins": 0})
        s["trades"] += 1
        if t.get("outcome"):
            s["resolved"] += 1
            s["sum"] += t["outcome"]["ret"]
            s["wins"] += t["outcome"]["ret"] > 0
    for s in out.values():
        s["mean_ret"] = s["sum"] / s["resolved"] if s["resolved"] else None
        s["win_rate"] = s["wins"] / s["resolved"] if s["resolved"] else None
        del s["sum"], s["wins"]
    return out


def run(state: dict, get_json, now: datetime, errors: list) -> dict:
    state.setdefault("start", MICRO_START)
    trades = state.setdefault("trades", [])
    have = {t["key"] for t in trades}
    mk = universe(get_json, errors)
    day = now.date().isoformat()

    # 1) diferencial real por tramo de volumen
    tiers = {">=1M": [], "100k-1M": [], "10k-100k": []}
    for m in mk:
        tiers[">=1M" if m["volume"] >= 1e6 else "100k-1M" if m["volume"] >= 1e5 else "10k-100k"].append((m["ask"] - m["bid"]) / 2)
    spreads = {k: {"n": len(v), "median_half_spread": sorted(v)[len(v) // 2] if v else None} for k, v in tiers.items()}

    # 2) candidatos de las reglas registradas (precio real: ask del lado comprado)
    cands = []
    for m in mk:
        end, start = _ts(m["end"]), _ts(m["start"])
        if not end:
            continue
        dte = (end - now).total_seconds() / 86400
        age = (now - start).total_seconds() / 86400 if start else 0
        ask_yes, ask_no = m["ask"], 1 - m["bid"]
        r = RULES.get("F4-C1")
        if r and 1 <= dte <= r["window_days"]:
            side = 0 if (m["bid"] + m["ask"]) / 2 >= 0.5 else 1
            a = ask_yes if side == 0 else ask_no
            if r["band"][0] <= a <= r["band"][1]:
                cands.append(("F4-C1", m, side, a))
        r = RULES.get("F4-N1")
        if r and m["out0"] == "Yes" and m["out1"] == "No" and age >= 3 and dte >= 7 and r["band"][0] <= (m["bid"] + m["ask"]) / 2 <= r["band"][1]:
            cands.append(("F4-N1", m, 1, ask_no))
    cands = [c for c in cands if f"{c[0]}:{c[1]['id']}" not in have]
    bk = books(sorted({c[1]["tok0"] if c[2] == 0 else c[1]["tok1"] for c in cands}), errors)
    for code, m, side, a in cands:
        b = bk.get(m["tok0"] if side == 0 else m["tok1"])
        if not b or b["usd"] < MIN_DEPTH_USD:
            continue
        a = b["ask"]  # precio ejecutable confirmado en el libro
        band = RULES[code]["band"]
        if code == "F4-C1" and not band[0] <= a <= band[1]:
            continue
        cost = a + fee(m["fee_rate"], a)
        trades.append({"key": f"{code}:{m['id']}", "code": code, "day": day, "question": m["question"], "slug": m["slug"],
                       "bought": m["out0"] if side == 0 else m["out1"], "price": a, "cost": cost, "depth_usd": round(b["usd"], 2),
                       "end": m["end"], "legs": [{"id": m["id"], "side": side}], "outcome": None})
        have.add(f"{code}:{m['id']}")

    # 3) arbitraje de suma en eventos negRisk (todas las patas abiertas, asks reales)
    vol = {}
    for m in mk:
        if m["neg_risk"] and m["event_id"]:
            vol[m["event_id"]] = vol.get(m["event_id"], 0) + m["volume"]
    scanned, found, sums_yes, sums_no = 0, [], [], []
    for eid in sorted(vol, key=lambda k: -vol[k])[:MAX_EVENTS]:
        try:
            e = get_json(f"{GAMMA}/events/{eid}")
        except Exception as ex:  # noqa: BLE001
            errors.append(f"micro evento {eid}: {ex}")
            continue
        legs_all = [x for x in e.get("markets") or [] if not x.get("closed")]
        legs = [_market({**x, "events": [{"id": eid, "slug": e.get("slug"), "title": e.get("title")}]}, strict=False) for x in legs_all]
        if len(legs) < 2 or any(x is None or not x["accepting"] for x in legs):
            continue  # falta alguna pata operable: la suma no es fiable
        scanned += 1
        n = len(legs)
        # todos los SÍ necesita una venta visible en cada pata; todos los NO, una compra visible (ask del NO = 1 − bid del SÍ)
        cy = sum(x["ask"] + fee(x["fee_rate"], x["ask"]) for x in legs) if all(x["ask"] and 0 < x["ask"] < 1 for x in legs) else None
        cn = sum((1 - x["bid"]) + fee(x["fee_rate"], 1 - x["bid"]) for x in legs) if all(x["bid"] and 0 < x["bid"] < 1 for x in legs) else None
        if cy is not None:
            sums_yes.append(cy)
        if cn is not None and n > 1:
            sums_no.append(cn / (n - 1))
        for side, cost, target in ((0, cy, 1.0), (1, cn, float(n - 1))):
            if cost is None or cost > target * (1 - ARB_MARGIN) or f"F4-A1:{eid}:{side}" in have:
                continue
            bk2 = books([x["tok0"] if side == 0 else x["tok1"] for x in legs], errors)
            got = [bk2.get(x["tok0"] if side == 0 else x["tok1"]) for x in legs]
            if any(g is None for g in got):
                continue
            cost2 = sum(g["ask"] + fee(x["fee_rate"], g["ask"]) for g, x in zip(got, legs))
            depth = min(g["usd"] / g["ask"] for g in got)  # acciones disponibles en la pata más fina
            if cost2 > target * (1 - ARB_MARGIN) or depth * cost2 < MIN_DEPTH_USD:
                continue
            row = {"key": f"F4-A1:{eid}:{side}", "code": "F4-A1", "day": day, "question": e.get("title", ""), "slug": e.get("slug", ""),
                   "bought": "todos los SÍ" if side == 0 else "todos los NO", "price": cost2 / n, "cost": cost2, "target": target,
                   "edge": target / cost2 - 1, "n_legs": n, "depth_usd": round(depth * cost2, 2), "augmented": bool(e.get("negRiskAugmented")),
                   "legs": [{"id": x["id"], "side": side} for x in legs], "outcome": None}
            trades.append(row)
            have.add(row["key"])
            found.append(row["key"])

    _resolve(get_json, trades, errors)
    med = lambda v: sorted(v)[len(v) // 2] if v else None  # noqa: E731
    today = {"day": day, "universe": len(mk), "events_scanned": scanned, "arb_found": len(found),
             "median_cost_all_yes": med(sums_yes), "min_cost_all_yes": min(sums_yes) if sums_yes else None,
             "median_cost_all_no_ratio": med(sums_no), "min_cost_all_no_ratio": min(sums_no) if sums_no else None,
             "spreads": spreads}
    state["daily"] = [x for x in state.get("daily", []) if x["day"] != day] + [today]
    state.update({"updated": now.isoformat(), "universe": len(mk), "spreads": spreads, "events_scanned": scanned,
                  "arb_found_today": len(found), "summary": _summary(trades), "rules": RULES})
    return state
