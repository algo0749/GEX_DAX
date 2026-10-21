"""
eurex_dax.py  —  Pipeline GEX/DEX para opciones sobre el DAX (Eurex · ODAX)
---------------------------------------------------------------------------
Proyecto GEX_DAX · Gamma y Delta Exposure de las opciones del DAX (Eurex).

    python3 eurex_dax.py                     # última sesión publicada por Eurex
    python3 eurex_dax.py --busdate 20261020  # una sesión concreta (Eurex guarda ~20)
    python3 eurex_dax.py --from-csv data/eurex_odax_20261020_raw.csv --busdate 20261020

Fuente: API JSON pública que usa la propia web de Eurex en la pestaña
"Prices/Quotes" del producto ODAX (id 70044):

    /api/v1/overallstatistics/70044?busdate=YYYYMMDD&filtertype=overview
        → lista de vencimientos vivos (semanales W y mensuales M) + fechas de sesión
    /api/v1/overallstatistics/70044?filtertype=detail&productdate=<vcto>&busdate=<sesión>&contracttype=<W|M>
        → por strike: openInterest, volume, dSettle (precio de liquidación diario)

Diferencias con el pipeline MEFF:
  · Eurex NO publica volatilidad implícita ni el precio del DAX → se calculan:
      - Forward de cada vencimiento por paridad put-call sobre los settlements.
      - Spot implícito = F · e^(−rT)  (el DAX es índice de rentabilidad total, sin dividendos).
      - Volatilidad implícita invirtiendo Black-76 con la opción OTM de cada strike
        (puts por debajo del forward, calls por encima) → misma vol para call y put.
  · Multiplicador ODAX = 5 €/punto.
  · Se INCLUYEN los semanales (en el DAX concentran mucha gamma de corto plazo).
  · T se mide desde la fecha de la sesión (busdate), no desde el día de ejecución.
  · Totales y desglose por vencimiento usan exactamente la misma función.

Outputs (mismo esquema que meff_gex_latest.json → reutilizable por el dashboard):
    data/eurex_odax_YYYYMMDD.csv     ← datos normalizados + forward + IV calculada
    data/dax_gex_YYYYMMDD.json
    data/dax_gex_latest.json
    data/dax_opciones_latest.json    ← IV y OI por strike (tabla del dashboard dax.html)
"""

import argparse
import glob
import json
import math
import os
from datetime import date, datetime

import numpy as np
import pandas as pd
import requests

import gex_calculator as gc

# ── Configuración ──────────────────────────────────────────────────────────────

BASE_URL      = "https://www.eurex.com/api/v1/overallstatistics/70044"
HEADERS       = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
ACCION        = "DAX"
MULTIPLICADOR = 5           # ODAX: 5 € por punto de índice
R             = gc.R        # misma tasa que el resto del proyecto
CARPETA       = "data"
N_PARIDAD     = 5           # strikes más cercanos al ATM usados para el forward
PRECIO_MIN    = 0.5         # settlements por debajo (en puntos) no se usan para IV
DIAS_HISTORICO = 60         # sesiones que se conservan en data/ (CSV y JSON con fecha)

MESES_ES = ["Ene", "Feb", "Mar", "Abr", "May", "Jun",
            "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]


# ── Descarga ───────────────────────────────────────────────────────────────────

def _get(params: dict) -> dict:
    r = requests.get(BASE_URL, params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def ultima_sesion() -> str:
    """Última fecha de sesión publicada (YYYYMMDD)."""
    hoy = datetime.today().strftime("%Y%m%d")
    j = _get({"busdate": hoy, "filtertype": "overview"})
    ultima = j["header"]["tradingDates"][0]          # "20-10-2026 12:00"
    return datetime.strptime(ultima[:10], "%d-%m-%Y").strftime("%Y%m%d")


def descargar(busdate: str) -> pd.DataFrame:
    ov = _get({"busdate": busdate, "filtertype": "overview"})
    if not ov.get("dataRows"):
        raise RuntimeError(f"Eurex no tiene datos para la sesión {busdate}")
    filas = []
    for v in ov["dataRows"]:
        det = _get({"filtertype": "detail", "productdate": v["date"],
                    "busdate": busdate, "contracttype": v["contractType"]})
        for x in det.get("dataRowsCall", []) + det.get("dataRowsPut", []):
            filas.append({
                "exp": v["date"], "ct": v["contractType"], "cp": x["callOrPut"][0],
                "strike": x["strike"], "oi": x["openInterest"],
                "vol": x["volume"], "settle": x["dSettle"],
            })
    return pd.DataFrame(filas)


# ── Black-76 ───────────────────────────────────────────────────────────────────

_ncdf = np.vectorize(lambda v: 0.5 * (1.0 + math.erf(v / math.sqrt(2.0))))


def black76(F, K, T, sigma, es_call, r=R):
    sq = sigma * np.sqrt(T)
    d1 = (np.log(F / K) + 0.5 * sigma**2 * T) / sq
    d2 = d1 - sq
    df = np.exp(-r * T)
    call = df * (F * _ncdf(d1) - K * _ncdf(d2))
    put  = df * (K * _ncdf(-d2) - F * _ncdf(-d1))
    return np.where(es_call, call, put)


def iv_black76(precio, F, K, T, es_call, r=R, lo=0.005, hi=3.0, iters=60):
    """Volatilidad implícita vectorizada por bisección. NaN si no es invertible."""
    precio, K, es_call = map(np.asarray, (precio, K, es_call))
    a = np.full(K.shape, lo); b = np.full(K.shape, hi)
    p_lo = black76(F, K, T, a, es_call, r); p_hi = black76(F, K, T, b, es_call, r)
    ok = (precio > p_lo) & (precio < p_hi)
    for _ in range(iters):
        m = 0.5 * (a + b)
        pm = black76(F, K, T, m, es_call, r)
        alto = pm > precio
        b = np.where(alto, m, b); a = np.where(alto, a, m)
    return np.where(ok, 0.5 * (a + b), np.nan)


# ── Forward, IV y preparación ─────────────────────────────────────────────────

def etiqueta_vcto(exp: str, ct: str) -> str:
    d = datetime.strptime(exp, "%Y%m%d")
    if ct == "M":
        return f"{MESES_ES[d.month - 1]}-{d:%y}"            # "Nov-26" (como MEFF)
    return f"{d:%d}-{MESES_ES[d.month - 1]}-{d:%y} (W)"     # "23-Oct-26 (W)"


def forward_paridad(g: pd.DataFrame, T: float, r=R):
    """F = K + (C − P)·e^(rT) sobre los N strikes con |C − P| mínimo."""
    piv = g.pivot_table(index="strike", columns="cp", values="settle", aggfunc="first")
    if not {"C", "P"} <= set(piv.columns):
        return None
    piv = piv[(piv["C"] > 0) & (piv["P"] > 0)]
    if piv.empty:
        return None
    piv = piv.assign(dif=(piv["C"] - piv["P"]).abs()).nsmallest(N_PARIDAD, "dif")
    f = piv.index.values + (piv["C"] - piv["P"]).values * math.exp(r * T)
    return float(np.median(f))


def preparar(raw: pd.DataFrame, busdate: str) -> tuple[pd.DataFrame, float]:
    """Devuelve (df por opción con T, F, sigma, …, spot implícito del DAX)."""
    d0 = datetime.strptime(busdate, "%Y%m%d").date()
    raw = raw.copy()
    for c in ("strike", "oi", "vol", "settle"):
        raw[c] = pd.to_numeric(raw[c], errors="coerce")
    raw["exp"] = raw["exp"].astype(str)

    bloques, spots = [], []
    for (exp, ct), g in raw.groupby(["exp", "ct"]):
        dias = (datetime.strptime(exp, "%Y%m%d").date() - d0).days
        if dias <= 0:
            continue
        T = dias / 365.0
        F = forward_paridad(g, T)
        if F is None:
            continue
        spots.append((dias, F * math.exp(-R * T)))

        # IV con la opción OTM de cada strike → una vol por strike
        otm = g[((g["cp"] == "P") & (g["strike"] < F)) | ((g["cp"] == "C") & (g["strike"] >= F))]
        otm = otm[otm["settle"] >= PRECIO_MIN]
        iv = iv_black76(otm["settle"].values, F, otm["strike"].values, T,
                        (otm["cp"] == "C").values)
        smile = pd.Series(iv, index=otm["strike"].values).dropna().sort_index()
        if len(smile) < 3:
            continue

        g = g.copy()
        # interpolación lineal en strike; fuera del rango, vol plana del extremo
        g["sigma"] = np.interp(g["strike"], smile.index.values, smile.values)
        g["T"], g["F"], g["dias"] = T, F, dias
        g["fecha_vencimiento"] = etiqueta_vcto(exp, ct)
        bloques.append(g)

    if not bloques:
        raise RuntimeError("No se pudo calcular forward/IV para ningún vencimiento.")
    df = pd.concat(bloques, ignore_index=True)

    # Spot implícito: mediana de los 3 vencimientos más cortos
    spots.sort()
    S0 = float(np.median([s for _, s in spots[:3]]))

    # Columnas que esperan las funciones de gex_calculator
    df["tipo"]       = np.where(df["cp"] == "C", "CALL", "PUT")
    df["strike_pts"] = df["strike"].astype(float)
    return df.reset_index(drop=True), S0


# ── GEX / DEX con las mismas funciones que el pipeline MEFF ───────────────────

def kpis_gex(df, S0):
    d = gc._calcular_gex_filas(df, S0, MULTIPLICADOR)
    gs = gc._agregar_por_strike(d)
    total = float(d["gex"].sum())
    return d, gs, {
        "spot":          S0,
        "call_wall":     float(gs.loc[gs["call_gex"].idxmax(), "strike"]),
        "put_wall":      float(gs.loc[gs["put_gex"].idxmin(), "strike"]),
        "zero_gamma":    gc._calcular_zero_gamma(S0, d, MULTIPLICADOR),
        "gex_total":     total,
        "regime":        "POSITIVO" if total >= 0 else "NEGATIVO",
        "multiplicador": MULTIPLICADOR,
    }


def kpis_dex(df, S0):
    d = gc._calcular_dex_filas(df, S0, MULTIPLICADOR)
    gs = gc._agregar_por_strike_dex(d)
    total = float(d["dex"].sum())
    return d, gs, {
        "spot":       S0,
        "call_wall":  float(gs.loc[gs["call_dex"].idxmax(), "strike"]),
        "put_wall":   float(gs.loc[gs["put_dex"].idxmin(), "strike"]),
        "zero_delta": gc._calcular_zero_delta(S0, d, MULTIPLICADOR),
        "dex_total":  total,
        "regime":     "LONG" if total >= 0 else "SHORT",
    }


def por_vencimiento(d: pd.DataFrame, col: str, S0: float) -> pd.DataFrame:
    """Desglose strike × vencimiento a partir de las MISMAS filas que el total."""
    t = d.pivot_table(index=["fecha_vencimiento", "strike_pts"], columns="tipo",
                      values=col, aggfunc="sum", fill_value=0.0)
    for c in ("CALL", "PUT"):
        if c not in t.columns:
            t[c] = 0.0
    t = t.reset_index().rename(columns={"strike_pts": "strike",
                                        "CALL": f"call_{col}", "PUT": f"put_{col}"})
    t[f"net_{col}"] = t[f"call_{col}"] + t[f"put_{col}"]
    t.insert(0, "accion", ACCION)
    t.insert(3, "spot", S0)
    return t[["accion", "fecha_vencimiento", "strike", "spot",
              f"call_{col}", f"put_{col}", f"net_{col}"]].round(2)


# ── Mantenimiento ─────────────────────────────────────────────────────────────

def mantener_ultimos(n: int = DIAS_HISTORICO):
    """Borra los CSV/JSON con fecha más antiguos para que el repositorio no crezca sin límite."""
    for patron in ("eurex_odax_[0-9]*.csv", "dax_gex_[0-9]*.json"):
        archivos = sorted(glob.glob(os.path.join(CARPETA, patron)))
        for f in archivos[:-n]:
            os.remove(f)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--busdate", help="Sesión YYYYMMDD (por defecto la última publicada)")
    ap.add_argument("--from-csv", help="Usar un CSV ya descargado en vez de la API")
    args = ap.parse_args()
    os.makedirs(CARPETA, exist_ok=True)

    if args.from_csv:
        if not args.busdate:
            ap.error("--from-csv requiere --busdate")
        busdate = args.busdate
        raw = pd.read_csv(args.from_csv, sep=";", dtype={"exp": str})
    else:
        busdate = args.busdate or ultima_sesion()
        print(f"Descargando ODAX de Eurex · sesión {busdate}")
        raw = descargar(busdate)
    print(f"  {len(raw)} filas · {raw['exp'].nunique()} vencimientos")

    df_all, S0 = preparar(raw, busdate)
    df = df_all[df_all["oi"] > 0].reset_index(drop=True)
    print(f"  DAX implícito (paridad put-call): {S0:,.1f}")

    # CSV normalizado (útil para el histórico y para auditar la IV)
    csv_path = f"{CARPETA}/eurex_odax_{busdate}.csv"
    (df_all[["exp", "ct", "fecha_vencimiento", "tipo", "strike", "oi", "vol",
         "settle", "F", "dias", "sigma"]]
       .assign(sigma=lambda x: (x["sigma"] * 100).round(2))
       .rename(columns={"sigma": "iv_pct", "oi": "posicion_abierta",
                        "vol": "volumen_contratos"})
       .to_csv(csv_path, sep=";", index=False, encoding="utf-8-sig"))

    dg, gs, kg = kpis_gex(df, S0)
    dd, ds, kd = kpis_dex(df, S0)

    zg = f"{kg['zero_gamma']:,.0f}" if kg["zero_gamma"] else "n/a"
    zd = f"{kd['zero_delta']:,.0f}" if kd["zero_delta"] else "n/a"
    print(f"  GEX total {kg['gex_total']:+,.0f} ({kg['regime']}) · "
          f"call wall {kg['call_wall']:,.0f} · put wall {kg['put_wall']:,.0f} · zero gamma {zg}")
    print(f"  DEX total {kd['dex_total']:+,.0f} ({kd['regime']}) · zero delta {zd}")

    orden = (df.drop_duplicates("fecha_vencimiento")
               .sort_values("dias")["fecha_vencimiento"].tolist())

    # KPIs de cada vencimiento (call/put wall y zero gamma/delta propios)
    kpis_vcto, kpis_dex_vcto = {}, {}
    for v in orden:
        sub = df[df["fecha_vencimiento"] == v]
        kpis_vcto[v] = kpis_gex(sub, S0)[2]
        kpis_dex_vcto[v] = kpis_dex(sub, S0)[2]

    def recs(t):
        return json.loads(t.to_json(orient="records", force_ascii=False))

    fb = datetime.strptime(busdate, "%Y%m%d").strftime("%d/%m/%y")
    salida = {
        "meta": {
            "fecha_boletin":     fb,
            "generado":          datetime.now().strftime("%Y-%m-%d %H:%M"),
            "tasa_libre_riesgo": R,
            "nota_tasa":         "Tipo BCE. Revisar periodicamente.",
            "fuente":            "Eurex (ODAX) · IV y spot implícitos calculados",
        },
        "subyacentes":          [ACCION],
        "vencimientos":         {ACCION: orden},
        "kpis_por_accion":      {ACCION: kg},
        "kpis_por_vcto":        {ACCION: kpis_vcto},
        "gex_por_strike_vcto":  recs(por_vencimiento(dg, "gex", S0)),
        "gex_por_strike":       recs(gs.assign(accion=ACCION, spot=S0)),
        "kpis_dex_por_accion":  {ACCION: kd},
        "kpis_dex_por_vcto":    {ACCION: kpis_dex_vcto},
        "dex_por_strike_vcto":  recs(por_vencimiento(dd, "dex", S0)),
        "dex_por_strike":       recs(ds.assign(accion=ACCION, spot=S0)),
    }
    for nombre in (f"{CARPETA}/dax_gex_{busdate}.json", f"{CARPETA}/dax_gex_latest.json"):
        with open(nombre, "w", encoding="utf-8") as f:
            json.dump(salida, f, ensure_ascii=False, indent=2)

    # Tabla por opción (IV y OI por strike) con el esquema de meff_opciones_latest.json
    opc = df_all[(df_all["oi"] > 0) | (df_all["vol"] > 0)]
    opciones = {
        "meta": {"fecha_boletin": fb, "generado": salida["meta"]["generado"], "fuente": "Eurex (ODAX)"},
        "subyacentes": [ACCION],
        "vencimientos": {ACCION: orden},
        "datos": [
            {"accion": ACCION, "tipo": r.tipo, "fecha_vencimiento": r.fecha_vencimiento,
             "strike": float(r.strike), "spot": round(S0, 2),
             "volatilidad_cierre": round(float(r.sigma) * 100, 2), "delta_cierre": None,
             "volumen_contratos": float(r.vol), "posicion_abierta": float(r.oi)}
            for r in opc.itertuples()
        ],
    }
    with open(f"{CARPETA}/dax_opciones_latest.json", "w", encoding="utf-8") as f:
        json.dump(opciones, f, ensure_ascii=False)
    mantener_ultimos()
    print(f"✓ {csv_path} · {CARPETA}/dax_gex_latest.json · {CARPETA}/dax_opciones_latest.json")


if __name__ == "__main__":
    main()
