"""
eurex_dax.py  —  Pipeline GEX/DEX del DAX y de sus 40 acciones (Eurex)
----------------------------------------------------------------------
Proyecto GEX_DAX · Gamma y Delta Exposure de las opciones del DAX (ODAX)
y de las opciones sobre cada acción del índice.

    python3 eurex_dax.py                     # última sesión: índice + 40 acciones
    python3 eurex_dax.py --solo-indice       # última sesión: solo el DAX (rápido)
    python3 eurex_dax.py --busdate 20261020  # una sesión concreta (Eurex guarda ~20)
    python3 eurex_dax.py --from-csv ejemplos/eurex_odax_20261020_raw.csv --busdate 20261020
    python3 eurex_dax.py --backfill          # sesiones que Eurex aún guarda (índice)

Fuente: API JSON pública que usa la web de Eurex en la pestaña "Prices/Quotes"
de cada producto (id de producto en la URL):

    /api/v1/overallstatistics/<id>?busdate=YYYYMMDD&filtertype=overview
        → vencimientos vivos (semanales W y mensuales M) + fechas de sesión
    /api/v1/overallstatistics/<id>?filtertype=detail&productdate=<vcto>&busdate=<sesión>&contracttype=<W|M>
        → por strike: openInterest, volume, dSettle (precio de liquidación diario)

Método:
  · Eurex NO publica volatilidad implícita ni el precio del subyacente → se calculan:
      - Forward de cada vencimiento por paridad put-call sobre los settlements
        (en las acciones recoge de forma aproximada los dividendos).
      - Spot = F · e^(−rT) de los vencimientos más cortos.
      - Volatilidad implícita invirtiendo Black-76 con la opción OTM de cada strike.
  · DAX: 5 €/punto. Acciones: 100 acciones/contrato (mini: 10). Las opciones sobre
    acciones son americanas en su mayoría → Black-Scholes es una aproximación
    (se usan solo opciones OTM para la IV, donde el ejercicio anticipado pesa poco).
  · Cada acción suma todos sus productos (americano, europeo, mini).
  · Se descartan las series ajustadas por operaciones corporativas (versionNumber ≠ 0).
  · Se incluyen los semanales. T se mide desde la fecha de la sesión.

Outputs:
    data/dax_gex_latest.json         ← índice + acciones (dashboard, pestañas γ y Δ)
    data/dax_opciones_latest.json    ← IV y OI por strike (vista tabla)
    data/dax_gex_YYYYMMDD.json       ← histórico del ÍNDICE (para no inflar el repo)
    data/eurex_odax_YYYYMMDD.csv     ← datos normalizados del índice (forward e IV)
    data/dax_regimen_historico.csv   ← una fila por sesión: DAX, GEX, régimen, niveles
"""

import argparse
import glob
import json
import math
import os
import time
from datetime import datetime

import numpy as np
import pandas as pd
import requests

import gex_calculator as gc

# ── Configuración ──────────────────────────────────────────────────────────────

BASE_URL       = "https://www.eurex.com/api/v1/overallstatistics"
HEADERS        = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
INDICE         = "DAX"
R              = gc.R        # misma tasa que el resto del proyecto
CARPETA        = "data"
N_PARIDAD      = 5           # strikes más cercanos al ATM usados para el forward
PRECIO_MIN_PCT = 0.00002     # settlements < 0,002 % del forward (0,5 pts en el DAX) no se usan para IV
DIAS_HISTORICO = 60          # sesiones del índice que se conservan en data/
PAUSA_API      = 0.3         # segundos entre peticiones a Eurex
RANGO_ACCIONES = 0.50        # strikes guardados para acciones: ±50 % del spot

# Subyacente → [(id de producto Eurex, multiplicador)]. El primero es el principal.
# Composición del DAX a octubre de 2026. Ids sacados de la web de Eurex
# (atributo data-i18n del bloque "option-prices" de cada página de producto).
SUBYACENTES = {
    "DAX":                   [(70044, 5)],
    "Adidas":                [(47634, 100), (47714, 100)],
    "Airbus":                [(50318, 100), (4575470, 100)],
    "Allianz":               [(47910, 100), (47926, 100)],
    "BASF":                  [(48322, 100), (48334, 100)],
    "Bayer":                 [(48352, 100), (48364, 100)],
    "Beiersdorf":            [(48466, 100)],
    "BMW":                   [(48584, 100), (48598, 100)],
    "Brenntag":              [(48610, 100)],
    "Commerzbank":           [(49152, 100), (49244, 100)],
    "Continental":           [(4708648, 100)],
    "Daimler Truck":         [(2916198, 100), (3701662, 100)],
    "Deutsche Bank":         [(49704, 100), (49714, 100)],
    "Deutsche Börse":        [(49674, 100), (49690, 100)],
    "Deutsche Telekom":      [(50164, 100), (50180, 100)],
    "DHL":                   [(49852, 100), (50046, 100)],
    "E.ON":                  [(50564, 100), (50656, 100)],
    "Fresenius":             [(51052, 100)],
    "Fresenius Medical Care": [(50932, 100)],
    "GEA":                   [(51116, 100), (4754166, 100)],
    "Hannover Rück":         [(51668, 100)],
    "Heidelberg Materials":  [(51520, 100)],
    "Henkel":                [(51592, 100)],
    "Hochtief":              [(51698, 100)],
    "Infineon":              [(51778, 100), (51766, 100)],
    "Mercedes-Benz":         [(2884630, 100), (2884634, 100)],
    "Merck":                 [(52808, 100), (52796, 100)],
    "MTU Aero Engines":      [(52820, 100), (4754654, 100), (4755358, 10)],
    "Münchener Rück":        [(52846, 100), (52860, 100), (4755264, 10)],
    "Qiagen":                [(53930, 100)],
    "Rheinmetall":           [(54110, 100), (3700108, 100), (4755346, 10)],
    "RWE":                   [(54440, 100), (54436, 100)],
    "SAP":                   [(54496, 100), (54584, 100)],
    "Scout24":               [(51140, 100)],
    "Siemens":               [(55006, 100), (55024, 100)],
    "Siemens Energy":        [(2293592, 100), (5376964, 100)],
    "Siemens Healthineers":  [(54954, 100)],
    "Symrise":               [(55444, 100)],
    "Volkswagen":            [(56148, 100), (56166, 100)],
    "Vonovia":               [(48012, 100)],
    "Zalando":               [(56470, 100), (3701654, 100)],
}

MESES_ES = ["Ene", "Feb", "Mar", "Abr", "May", "Jun",
            "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]


# ── Descarga ───────────────────────────────────────────────────────────────────

def _get(pid: int, params: dict) -> dict:
    time.sleep(PAUSA_API)
    for intento in range(3):
        try:
            r = requests.get(f"{BASE_URL}/{pid}", params=params, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if intento == 2:
                raise
            time.sleep(2 * (intento + 1))


def sesiones_disponibles() -> list[str]:
    """Sesiones que Eurex aún publica (YYYYMMDD), de la más reciente a la más antigua."""
    hoy = datetime.today().strftime("%Y%m%d")
    j = _get(SUBYACENTES[INDICE][0][0], {"busdate": hoy, "filtertype": "overview"})
    return [datetime.strptime(t[:10], "%d-%m-%Y").strftime("%Y%m%d")   # "20-10-2026 12:00"
            for t in j["header"]["tradingDates"]]


def ultima_sesion() -> str:
    return sesiones_disponibles()[0]


def descargar_producto(pid: int, mult: int, busdate: str) -> pd.DataFrame:
    ov = _get(pid, {"busdate": busdate, "filtertype": "overview"})
    filas = []
    for v in ov.get("dataRows") or []:
        if not (v.get("callOpenInterest") or v.get("putOpenInterest")
                or v.get("callVolume") or v.get("putVolume")):
            continue                                   # vencimiento sin posición ni volumen
        det = _get(pid, {"filtertype": "detail", "productdate": v["date"],
                         "busdate": busdate, "contracttype": v["contractType"]})
        for x in (det.get("dataRowsCall") or []) + (det.get("dataRowsPut") or []):
            if x.get("versionNumber", 0) != 0:
                continue                               # serie ajustada (otro tamaño)
            filas.append({
                "prod": pid, "mult": mult,
                "exp": v["date"], "ct": v["contractType"], "cp": x["callOrPut"][0],
                "strike": x["strike"], "oi": x["openInterest"],
                "vol": x["volume"], "settle": x["dSettle"],
            })
    return pd.DataFrame(filas)


def descargar(nombre: str, busdate: str) -> pd.DataFrame:
    partes = [descargar_producto(pid, mult, busdate) for pid, mult in SUBYACENTES[nombre]]
    partes = [p for p in partes if not p.empty]
    if not partes:
        raise RuntimeError(f"Eurex no tiene datos de {nombre} para la sesión {busdate}")
    return pd.concat(partes, ignore_index=True)


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
    """Devuelve (df por opción con T, F, sigma…, spot implícito).

    Forward e IV se calculan por producto y vencimiento; la columna `oi` pasa a
    estar en unidades del subyacente (contratos × multiplicador) y `oi_contratos`
    conserva el número de contratos."""
    d0 = datetime.strptime(busdate, "%Y%m%d").date()
    raw = raw.copy()
    for c in ("strike", "oi", "vol", "settle", "mult"):
        if c not in raw.columns and c == "mult":
            raw["mult"] = SUBYACENTES[INDICE][0][1]
        raw[c] = pd.to_numeric(raw[c], errors="coerce")
    if "prod" not in raw.columns:
        raw["prod"] = SUBYACENTES[INDICE][0][0]
    raw["exp"] = raw["exp"].astype(str)
    principal = raw["prod"].iloc[0]

    bloques, spots = [], []
    for (prod, exp, ct), g in raw.groupby(["prod", "exp", "ct"]):
        dias = (datetime.strptime(exp, "%Y%m%d").date() - d0).days
        if dias <= 0:
            continue
        T = dias / 365.0
        F = forward_paridad(g, T)
        if F is None or F <= 0:
            continue
        spots.append((prod != principal, dias, F * math.exp(-R * T)))

        # IV con la opción OTM de cada strike → una vol por strike
        otm = g[((g["cp"] == "P") & (g["strike"] < F)) | ((g["cp"] == "C") & (g["strike"] >= F))]
        otm = otm[otm["settle"] >= max(PRECIO_MIN_PCT * F, 0.01)]
        iv = iv_black76(otm["settle"].values, F, otm["strike"].values, T,
                        (otm["cp"] == "C").values)
        smile = pd.Series(iv, index=otm["strike"].values).dropna().sort_index()
        smile = smile[~smile.index.duplicated()]
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

    # Spot implícito: mediana de los 3 vencimientos más cortos (producto principal primero)
    spots.sort()
    S0 = float(np.median([s for _, _, s in spots[:3]]))

    # Columnas que esperan las funciones de gex_calculator
    df["tipo"]         = np.where(df["cp"] == "C", "CALL", "PUT")
    df["strike_pts"]   = df["strike"].astype(float)
    df["oi_contratos"] = df["oi"]
    df["oi"]           = df["oi"] * df["mult"]
    return df.reset_index(drop=True), S0


# ── GEX / DEX con las mismas funciones que el pipeline MEFF ───────────────────
# El multiplicador ya va dentro de `oi`, así que a gex_calculator se le pasa 1.

def kpis_gex(df, S0, mult_info):
    d = gc._calcular_gex_filas(df, S0, 1)
    gs = gc._agregar_por_strike(d)
    total = float(d["gex"].sum())
    return d, gs, {
        "spot":          S0,
        "call_wall":     float(gs.loc[gs["call_gex"].idxmax(), "strike"]),
        "put_wall":      float(gs.loc[gs["put_gex"].idxmin(), "strike"]),
        "zero_gamma":    gc._calcular_zero_gamma(S0, d, 1),
        "gex_total":     total,
        "gex_call":      float(d.loc[d["tipo"] == "CALL", "gex"].sum()),
        "gex_put":       float(d.loc[d["tipo"] == "PUT", "gex"].sum()),
        "regime":        "POSITIVO" if total >= 0 else "NEGATIVO",
        "multiplicador": mult_info,
    }


def kpis_dex(df, S0):
    d = gc._calcular_dex_filas(df, S0, 1)
    gs = gc._agregar_por_strike_dex(d)
    total = float(d["dex"].sum())
    return d, gs, {
        "spot":       S0,
        "call_wall":  float(gs.loc[gs["call_dex"].idxmax(), "strike"]),
        "put_wall":   float(gs.loc[gs["put_dex"].idxmin(), "strike"]),
        "zero_delta": gc._calcular_zero_delta(S0, d, 1),
        "dex_total":  total,
        "dex_call":   float(d.loc[d["tipo"] == "CALL", "dex"].sum()),
        "dex_put":    float(d.loc[d["tipo"] == "PUT", "dex"].sum()),
        "regime":     "LONG" if total >= 0 else "SHORT",
    }


def por_vencimiento(d: pd.DataFrame, col: str, S0: float, nombre: str) -> pd.DataFrame:
    """Desglose strike × vencimiento a partir de las MISMAS filas que el total."""
    t = d.pivot_table(index=["fecha_vencimiento", "strike_pts"], columns="tipo",
                      values=col, aggfunc="sum", fill_value=0.0)
    for c in ("CALL", "PUT"):
        if c not in t.columns:
            t[c] = 0.0
    t = t.reset_index().rename(columns={"strike_pts": "strike",
                                        "CALL": f"call_{col}", "PUT": f"put_{col}"})
    t[f"net_{col}"] = t[f"call_{col}"] + t[f"put_{col}"]
    t.insert(0, "accion", nombre)
    t.insert(3, "spot", S0)
    return t[["accion", "fecha_vencimiento", "strike", "spot",
              f"call_{col}", f"put_{col}", f"net_{col}"]].round(2)


def recs(t: pd.DataFrame) -> list:
    return json.loads(t.to_json(orient="records", force_ascii=False))


def calcular_subyacente(nombre: str, raw: pd.DataFrame, busdate: str) -> dict:
    """Todo lo que el dashboard necesita de un subyacente."""
    df_all, S0 = preparar(raw, busdate)
    df = df_all[df_all["oi"] > 0].reset_index(drop=True)
    if df.empty:
        raise RuntimeError("sin posición abierta")
    mult_info = SUBYACENTES[nombre][0][1]

    dg, gs, kg = kpis_gex(df, S0, mult_info)
    dd, ds, kd = kpis_dex(df, S0)
    orden = (df.drop_duplicates("fecha_vencimiento")
               .sort_values("dias")["fecha_vencimiento"].tolist())
    kv, kdv = {}, {}
    for v in orden:
        sub = df[df["fecha_vencimiento"] == v]
        kv[v] = kpis_gex(sub, S0, mult_info)[2]
        kdv[v] = kpis_dex(sub, S0)[2]

    def recorta(t):
        """Quita strikes sin exposición y, en acciones, los muy alejados del spot.
        También quita la columna spot por fila (el dashboard usa la de los KPIs)."""
        if nombre != INDICE:
            t = t[(t["strike"] >= S0 * (1 - RANGO_ACCIONES)) & (t["strike"] <= S0 * (1 + RANGO_ACCIONES))]
        num = [c for c in t.columns if c.endswith(("_gex", "_dex"))]
        if num:
            t = t[(t[num].abs() > 0.5).any(axis=1)]
            t = t.assign(**{c: t[c].round(0) for c in num})
        return t.drop(columns=["spot"], errors="ignore")

    opc = df_all[df_all["oi_contratos"] > 0]
    opc = opc.groupby(["tipo", "fecha_vencimiento", "strike"], as_index=False).agg(
        sigma=("sigma", "first"), vol=("vol", "sum"), oi=("oi_contratos", "sum"))
    opc = recorta(opc)
    return {
        "nombre": nombre, "spot": S0, "orden": orden, "df_all": df_all,
        "kg": kg, "kd": kd, "kv": kv, "kdv": kdv,
        "gex_vcto": recs(recorta(por_vencimiento(dg, "gex", S0, nombre))),
        "gex":      recs(recorta(gs.assign(accion=nombre))),
        "dex_vcto": recs(recorta(por_vencimiento(dd, "dex", S0, nombre))),
        "dex":      recs(recorta(ds.assign(accion=nombre))),
        "opciones": [
            {"accion": nombre, "tipo": r.tipo, "fecha_vencimiento": r.fecha_vencimiento,
             "strike": float(r.strike), "volatilidad_cierre": round(float(r.sigma) * 100, 1),
             "posicion_abierta": int(r.oi)}
            for r in opc.itertuples()
        ],
    }


def json_gex(resultados: list, busdate: str) -> dict:
    fb = datetime.strptime(busdate, "%Y%m%d").strftime("%d/%m/%y")
    return {
        "meta": {
            "fecha_boletin":     fb,
            "generado":          datetime.now().strftime("%Y-%m-%d %H:%M"),
            "tasa_libre_riesgo": R,
            "nota_tasa":         "Tipo BCE. Revisar periodicamente.",
            "fuente":            "Eurex · IV y spot implícitos calculados",
        },
        "subyacentes":         [x["nombre"] for x in resultados],
        "vencimientos":        {x["nombre"]: x["orden"] for x in resultados},
        "kpis_por_accion":     {x["nombre"]: x["kg"] for x in resultados},
        "kpis_por_vcto":       {x["nombre"]: x["kv"] for x in resultados},
        "gex_por_strike_vcto": [r for x in resultados for r in x["gex_vcto"]],
        "gex_por_strike":      [r for x in resultados for r in x["gex"]],
        "kpis_dex_por_accion": {x["nombre"]: x["kd"] for x in resultados},
        "kpis_dex_por_vcto":   {x["nombre"]: x["kdv"] for x in resultados},
        "dex_por_strike_vcto": [r for x in resultados for r in x["dex_vcto"]],
        "dex_por_strike":      [r for x in resultados for r in x["dex"]],
    }


def _guardar(ruta: str, obj: dict, indent=None):
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent,
                  separators=None if indent else (",", ":"))


# ── Sesión completa ───────────────────────────────────────────────────────────

def procesar(busdate: str, latest: bool = True, solo_indice: bool = False,
             raw_indice: pd.DataFrame | None = None):
    """Calcula una sesión y escribe sus archivos.

    El índice es obligatorio (si falla, error). Una acción que falle se omite.
    latest=True actualiza los *_latest.json que lee el dashboard."""
    nombres = [INDICE] if solo_indice else list(SUBYACENTES)
    resultados, omitidas = [], []
    for nombre in nombres:
        try:
            raw = raw_indice if (nombre == INDICE and raw_indice is not None) else descargar(nombre, busdate)
            x = calcular_subyacente(nombre, raw, busdate)
        except Exception as exc:
            if nombre == INDICE:
                raise
            omitidas.append(nombre)
            print(f"  · {nombre}: omitida ({exc})")
            continue
        zg = x["kg"]["zero_gamma"]
        print(f"  · {nombre:<22} spot {x['spot']:>10,.2f} · GEX {x['kg']['gex_total'] / 1e6:>+12,.1f} M€ · "
              f"zero γ {'n/a' if zg is None else f'{zg:,.2f}'}")
        resultados.append(x)

    indice = resultados[0]
    # CSV normalizado y JSON con fecha: solo el índice (el repo no crece con las acciones)
    (indice["df_all"][["exp", "ct", "fecha_vencimiento", "tipo", "strike", "oi_contratos", "vol",
                       "settle", "F", "dias", "sigma"]]
       .assign(sigma=lambda x: (x["sigma"] * 100).round(2))
       .rename(columns={"sigma": "iv_pct", "oi_contratos": "posicion_abierta",
                        "vol": "volumen_contratos"})
       .to_csv(f"{CARPETA}/eurex_odax_{busdate}.csv", sep=";", index=False, encoding="utf-8-sig"))
    _guardar(f"{CARPETA}/dax_gex_{busdate}.json", json_gex([indice], busdate), indent=1)

    if latest:
        _guardar(f"{CARPETA}/dax_gex_latest.json", json_gex(resultados, busdate))
        gj = json_gex(resultados, busdate)
        _guardar(f"{CARPETA}/dax_opciones_latest.json", {
            "meta": {"fecha_boletin": gj["meta"]["fecha_boletin"], "generado": gj["meta"]["generado"],
                     "fuente": "Eurex"},
            "subyacentes": gj["subyacentes"], "vencimientos": gj["vencimientos"],
            "datos": [r for x in resultados for r in x["opciones"]],
        })
    mantener_ultimos()
    print(f"✓ {busdate} · {len(resultados)} subyacentes"
          + (f" · omitidas: {', '.join(omitidas)}" if omitidas else "")
          + (" · dashboard actualizado" if latest else ""))


# ── Mantenimiento ─────────────────────────────────────────────────────────────

def mantener_ultimos(n: int = DIAS_HISTORICO):
    """Borra los CSV/JSON con fecha más antiguos para que el repositorio no crezca sin límite."""
    for patron in ("eurex_odax_[0-9]*.csv", "dax_gex_[0-9]*.json"):
        archivos = sorted(glob.glob(os.path.join(CARPETA, patron)))
        for f in archivos[:-n]:
            os.remove(f)


def construir_resumen():
    """data/dax_regimen_historico.csv: una fila por sesión guardada en data/ (índice).

    Los niveles de la sesión D salen de su cierre, así que sirven para operar
    la sesión SIGUIENTE (D+1)."""
    filas = []
    for f in sorted(glob.glob(os.path.join(CARPETA, "dax_gex_[0-9]*.json"))):
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
            k, kd = d["kpis_por_accion"][INDICE], d["kpis_dex_por_accion"][INDICE]
        except (KeyError, ValueError, OSError) as exc:
            print(f"  AVISO: {f} ignorado en el resumen ({exc})")
            continue
        spot, zg = k["spot"], k.get("zero_gamma")
        filas.append({
            "sesion":           datetime.strptime(d["meta"]["fecha_boletin"], "%d/%m/%y").strftime("%Y-%m-%d"),
            "dax":              round(spot, 1),
            "gex_total_meur":   round(k["gex_total"] / 1e6, 1),
            "regimen_gex":      k["regime"],
            "zero_gamma":       None if zg is None else round(zg),
            "dax_vs_zero_gamma": None if zg is None else ("ENCIMA" if spot >= zg else "DEBAJO"),
            "dist_zero_gamma_pct": None if zg is None else round((spot / zg - 1) * 100, 2),
            "call_wall":        k["call_wall"],
            "put_wall":         k["put_wall"],
            "dex_total_meur":   round(kd["dex_total"] / 1e6, 1),
            "regimen_dex":      kd["regime"],
            "zero_delta":       None if kd.get("zero_delta") is None else round(kd["zero_delta"]),
        })
    if filas:
        pd.DataFrame(filas).to_csv(os.path.join(CARPETA, "dax_regimen_historico.csv"),
                                   sep=";", index=False, encoding="utf-8-sig")
        print(f"  Resumen: {CARPETA}/dax_regimen_historico.csv ({len(filas)} sesiones)")


def backfill():
    """Procesa (solo el índice) las sesiones que Eurex aún guarda y no estén en data/."""
    sesiones = sesiones_disponibles()
    pendientes = [s for s in reversed(sesiones)
                  if not os.path.exists(os.path.join(CARPETA, f"dax_gex_{s}.json"))]
    print(f"Eurex guarda {len(sesiones)} sesiones ({sesiones[-1]} → {sesiones[0]}); "
          f"faltan {len(pendientes)}")
    fallos = []
    for s in pendientes:
        try:
            print(f"\n── Sesión {s}")
            procesar(s, latest=False, solo_indice=True)
        except Exception as exc:                     # una sesión mala no para las demás
            print(f"  ERROR en {s}: {exc}")
            fallos.append(s)
    construir_resumen()
    if fallos:
        print(f"\nSesiones con error: {', '.join(fallos)}")
    if fallos and len(fallos) == len(pendientes):
        raise SystemExit(1)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--busdate", help="Sesión YYYYMMDD (por defecto la última publicada)")
    ap.add_argument("--from-csv", help="CSV del índice ya descargado (implica --solo-indice)")
    ap.add_argument("--solo-indice", action="store_true", help="Calcular solo el DAX, sin acciones")
    ap.add_argument("--backfill", action="store_true",
                    help="Descargar (solo índice) las sesiones que Eurex aún guarda y no estén en data/")
    args = ap.parse_args()
    os.makedirs(CARPETA, exist_ok=True)

    if args.backfill:
        backfill()
        return

    if args.from_csv:
        if not args.busdate:
            ap.error("--from-csv requiere --busdate")
        raw = pd.read_csv(args.from_csv, sep=";", dtype={"exp": str})
        procesar(args.busdate, latest=True, solo_indice=True, raw_indice=raw)
    else:
        busdate = args.busdate or ultima_sesion()
        print(f"Descargando Eurex · sesión {busdate}")
        procesar(busdate, latest=True, solo_indice=args.solo_indice)
    construir_resumen()


if __name__ == "__main__":
    main()
