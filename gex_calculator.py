"""
gex_calculator.py
-----------------
Calcula Gamma Exposure (GEX) y Delta Exposure (DEX) y KPIs derivados
desde el CSV de meff_opciones.py.

Parámetros del modelo:
  R            = 0.025  (BCE, tasa libre de riesgo)
  SWEEP_MARGIN = 0.15   (±15% del spot para buscar Zero Gamma / Zero Delta)
  N_SWEEP      = 600    (resolución del barrido)

Multiplicadores por contrato:
  MINI IBEX-35 → 1 €/punto
  Resto        → 100 acciones/contrato

Convenio GEX (SpotGamma):
  GEX(K) = OI_call × Γ × S² × mult  −  OI_put × Γ × S² × mult
  Dealers asumidos short en ambos lados.
  GEX positivo → dealers long gamma → volatilidad amortiguada.
  GEX negativo → dealers short gamma → volatilidad amplificada.

Convenio DEX:
  DEX(K) = OI_call × Δ_call × S × mult  +  OI_put × Δ_put × S × mult
  Δ_call = N(d1) ∈ (0,1)   Δ_put = N(d1) − 1 ∈ (−1,0)
  DEX positivo (LONG)  → dealers son long delta → venden rallies (techo).
  DEX negativo (SHORT) → dealers son short delta → compran caídas (suelo).
"""

import re
import math
import numpy as np
import pandas as pd
from datetime import date, datetime

# ── Parámetros ────────────────────────────────────────────────────────────────
R            = 0.025
SWEEP_MARGIN = 0.15
N_SWEEP      = 600

MESES = {
    "ene": 1, "jan": 1, "feb": 2, "mar": 3, "abr": 4, "apr": 4,
    "may": 5, "jun": 6, "jul": 7, "ago": 8, "aug": 8,
    "sep": 9, "oct": 10, "nov": 11, "dic": 12, "dec": 12,
}

# ── Parseo de números ─────────────────────────────────────────────────────────

def parse_es(s) -> float:
    """
    Convierte string numérico en formato español/CSV mixto a float.

    '17.500'   → 17500.0   (miles: punto seguido de EXACTAMENTE 3 dígitos)
    '17622.7'  → 17622.7   (decimal anglosajón: otro número de decimales)
    '37,47'    → 37.47     (decimal español con coma)
    '2.116,60' → 2116.60   (mixto)
    '-'        → NaN
    """
    if pd.isna(s):
        return float("nan")
    s = str(s).strip()
    if s in ("", "-", "–", "—", "nan", "N/A"):
        return float("nan")
    try:
        if "," in s:
            return float(s.replace(".", "").replace(",", "."))
        elif "." in s:
            partes = s.split(".")
            if len(partes) == 2 and len(partes[1]) == 3:
                return float(s.replace(".", ""))
            return float(s)
        return float(s)
    except ValueError:
        return float("nan")


# ── Fechas ───────────────────────────────────────────────────────────────────

_fv_cache: dict = {}

def _tercer_viernes(anio: int, mes: int) -> date:
    key = (anio, mes)
    if key not in _fv_cache:
        d = date(anio, mes, 1)
        dias = (4 - d.weekday()) % 7
        _fv_cache[key] = date(anio, mes, 1 + dias + 14)
    return _fv_cache[key]


def _parsear_vencimiento(texto: str) -> date | None:
    """'May-26' → tercer viernes de mayo 2026."""
    m = re.match(r"([A-Za-z]+)-(\d{2,4})", str(texto).strip())
    if not m:
        return None
    mes = MESES.get(m.group(1).lower()[:3])
    if not mes:
        return None
    anio = int(m.group(2))
    anio = anio if anio > 100 else 2000 + anio
    try:
        return _tercer_viernes(anio, mes)
    except ValueError:
        return None


# ── Normal CDF (usada por Delta BS) ──────────────────────────────────────────

def _ncdf(x) -> np.ndarray:
    """
    CDF de la normal estándar. Compatible con escalares float y arrays numpy.
    Usa math.erf (stdlib) a través de np.vectorize — sin dependencias externas.
    """
    return np.vectorize(lambda v: 0.5 * (1.0 + math.erf(v / math.sqrt(2.0))))(
        np.asarray(x, dtype=float)
    )


# ── Modelo Black-Scholes: Gamma ───────────────────────────────────────────────

def bs_gamma(S: float, K: float, T: float, sigma: float, r: float = R) -> float:
    """Gamma BS escalar (idéntica para CALL y PUT). Usada en cálculos puntuales."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    try:
        d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
        return float(np.exp(-0.5 * d1**2) / (np.sqrt(2 * np.pi) * S * sigma * np.sqrt(T)))
    except Exception:
        return 0.0


def _bs_gamma_vec(S: float,
                  K: np.ndarray,
                  T: np.ndarray,
                  sigma: np.ndarray,
                  r: float = R) -> np.ndarray:
    """
    Gamma BS vectorizada para arrays numpy.

    Procesa todo el DataFrame de una vez en lugar de fila a fila.
    Es ~50x más rápida que el equivalente con iterrows() en el barrido de zero-gamma.
    """
    gamma = np.zeros(len(K))
    valid = (T > 0) & (sigma > 0) & (S > 0) & (K > 0)
    if not valid.any():
        return gamma
    Kv, Tv, sv = K[valid], T[valid], sigma[valid]
    d1 = (np.log(S / Kv) + (r + 0.5 * sv ** 2) * Tv) / (sv * np.sqrt(Tv))
    gamma[valid] = np.exp(-0.5 * d1 ** 2) / (np.sqrt(2 * np.pi) * S * sv * np.sqrt(Tv))
    return gamma


# ── Modelo Black-Scholes: Delta ───────────────────────────────────────────────

def bs_delta(S: float, K: float, T: float, sigma: float,
             tipo: str = "CALL", r: float = R) -> float:
    """
    Delta BS escalar.
    Δ_call = N(d1)       ∈ (0, 1)
    Δ_put  = N(d1) − 1  ∈ (−1, 0)
    """
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    try:
        d1  = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
        nd1 = float(_ncdf(d1))
        return nd1 if tipo.upper() == "CALL" else nd1 - 1.0
    except Exception:
        return 0.0


def _bs_delta_vec(S: float,
                  K: np.ndarray,
                  T: np.ndarray,
                  sigma: np.ndarray,
                  tipos: np.ndarray,
                  r: float = R) -> np.ndarray:
    """
    Delta BS vectorizada para arrays numpy.
    tipos: array de strings 'CALL'/'PUT' (en mayúsculas).

    Implementación vectorizada — misma velocidad que _bs_gamma_vec.
    """
    delta = np.zeros(len(K))
    valid = (T > 0) & (sigma > 0) & (S > 0) & (K > 0)
    if not valid.any():
        return delta
    Kv, Tv, sv = K[valid], T[valid], sigma[valid]
    d1   = (np.log(S / Kv) + (r + 0.5 * sv**2) * Tv) / (sv * np.sqrt(Tv))
    nd1  = _ncdf(d1)
    is_call = tipos[valid] == "CALL"
    delta[valid] = np.where(is_call, nd1, nd1 - 1.0)
    return delta


# ── Multiplicador ─────────────────────────────────────────────────────────────

def get_multiplicador(accion: str) -> int:
    return 1 if "IBEX" in accion.upper() else 100


# ── Preparación del DataFrame ─────────────────────────────────────────────────

def _preparar(df_raw: pd.DataFrame, accion: str, hoy: date) -> pd.DataFrame:
    """Filtra y parsea el DataFrame para un subyacente dado."""
    df = df_raw[df_raw["accion"].str.upper() == accion.upper()].copy()
    if df.empty:
        return df

    df["strike_pts"] = df["strike"].apply(parse_es)
    df["oi"]         = df["posicion_abierta"].apply(parse_es)
    df["sigma"]      = df["volatilidad_cierre"].apply(parse_es) / 100
    df["spot_val"]   = df["spot"].apply(parse_es)
    df["fv_date"]    = df["fecha_vencimiento"].apply(_parsear_vencimiento)
    df["T"]          = df["fv_date"].apply(
        lambda d: max((d - hoy).days, 0) / 365.0 if d is not None else float("nan")
    )

    mask = (
        (df["oi"] > 0)         &
        df["oi"].notna()        &
        df["strike_pts"].notna() &
        df["sigma"].notna()     &
        (df["sigma"] > 0)       &
        (df["T"] > 0)           &
        df["tipo"].str.upper().isin(["CALL", "PUT"])
    )
    return df[mask].copy()


# ── GEX por fila y por strike ─────────────────────────────────────────────────

def _calcular_gex_filas(df: pd.DataFrame, S0: float, mult: int) -> pd.DataFrame:
    """
    Calcula gamma y GEX para cada fila.
    Usa la versión vectorizada de bs_gamma para mayor rendimiento.
    """
    df = df.copy()
    K     = df["strike_pts"].values.astype(float)
    T     = df["T"].values.astype(float)
    sigma = df["sigma"].values.astype(float)
    signs = np.where(df["tipo"].str.upper() == "CALL", 1.0, -1.0)

    df["gamma_bs"] = _bs_gamma_vec(S0, K, T, sigma)
    df["gex"]      = signs * df["oi"].values * df["gamma_bs"].values * S0 ** 2 * mult
    return df


def _agregar_por_strike(df: pd.DataFrame) -> pd.DataFrame:
    gs = (
        df.groupby(["strike_pts", "tipo"])["gex"]
        .sum()
        .unstack(fill_value=0)
        .rename(columns={"CALL": "gex_call", "PUT": "gex_put"})
    )
    for col in ("gex_call", "gex_put"):
        if col not in gs.columns:
            gs[col] = 0.0
    gs["gex_neto"] = gs["gex_call"] + gs["gex_put"]
    return gs.reset_index().sort_values("strike_pts").rename(
        columns={"strike_pts": "strike", "gex_call": "call_gex",
                 "gex_put": "put_gex", "gex_neto": "net_gex"}
    )


# ── Zero Gamma (barrido vectorizado) ─────────────────────────────────────────

def _calcular_zero_gamma(S0: float, df: pd.DataFrame, mult: int):
    """
    Busca el nivel de spot donde el GEX neto cruza cero mediante un barrido
    de N_SWEEP puntos en el rango S0 × (1 ± SWEEP_MARGIN).

    Implementación vectorizada: extrae arrays numpy una sola vez y opera
    en batch en lugar de iterar fila a fila. Resultado idéntico, ~50x más rápido.

    Devuelve el precio interpolado en el cruce más cercano al spot actual,
    o None si no hay cruce en el rango.
    """
    K     = df["strike_pts"].values.astype(float)
    T     = df["T"].values.astype(float)
    sigma = df["sigma"].values.astype(float)
    oi    = df["oi"].values.astype(float)
    signs = np.where(df["tipo"].str.upper() == "CALL", 1.0, -1.0)

    spots = np.linspace(S0 * (1 - SWEEP_MARGIN), S0 * (1 + SWEEP_MARGIN), N_SWEEP)

    gex_s = np.array([
        float((signs * oi * _bs_gamma_vec(s, K, T, sigma) * s ** 2 * mult).sum())
        for s in spots
    ])

    cruces = np.where(np.diff(np.sign(gex_s)))[0]
    if not len(cruces):
        return None

    i = cruces[np.argmin(np.abs(spots[cruces] - S0))]
    x0, x1, y0, y1 = spots[i], spots[i + 1], gex_s[i], gex_s[i + 1]
    return float(x0 - y0 * (x1 - x0) / (y1 - y0))


# ── DEX por fila y por strike ─────────────────────────────────────────────────

def _calcular_dex_filas(df: pd.DataFrame, S0: float, mult: int) -> pd.DataFrame:
    """
    Calcula delta BS y DEX para cada fila.
    El signo del delta ya incluye la dirección: Δ_call > 0, Δ_put < 0.
    Usa la versión vectorizada de bs_delta para mayor rendimiento.
    """
    df    = df.copy()
    K     = df["strike_pts"].values.astype(float)
    T     = df["T"].values.astype(float)
    sigma = df["sigma"].values.astype(float)
    tipos = df["tipo"].str.upper().values

    df["delta_bs"] = _bs_delta_vec(S0, K, T, sigma, tipos)
    # DEX = OI × Δ × S × mult  (signo ya incluido en delta)
    df["dex"] = df["oi"].values * df["delta_bs"].values * S0 * mult
    return df


def _agregar_por_strike_dex(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["tipo"] = df["tipo"].str.upper()
    gs = (
        df.groupby(["strike_pts", "tipo"])["dex"]
        .sum()
        .unstack(fill_value=0)
        .rename(columns={"CALL": "dex_call", "PUT": "dex_put"})
    )
    for col in ("dex_call", "dex_put"):
        if col not in gs.columns:
            gs[col] = 0.0
    gs["dex_neto"] = gs["dex_call"] + gs["dex_put"]
    return gs.reset_index().sort_values("strike_pts").rename(
        columns={"strike_pts": "strike", "dex_call": "call_dex",
                 "dex_put": "put_dex", "dex_neto": "net_dex"}
    )


# ── Zero Delta (barrido vectorizado) ─────────────────────────────────────────

def _calcular_zero_delta(S0: float, df: pd.DataFrame, mult: int):
    """
    Busca el nivel de spot donde el DEX neto cruza cero mediante un barrido
    de N_SWEEP puntos en el rango S0 × (1 ± SWEEP_MARGIN).

    Misma lógica que _calcular_zero_gamma pero aplicada sobre la curva DEX.
    Devuelve None si no hay cruce en el rango.
    """
    K     = df["strike_pts"].values.astype(float)
    T     = df["T"].values.astype(float)
    sigma = df["sigma"].values.astype(float)
    oi    = df["oi"].values.astype(float)
    tipos = df["tipo"].str.upper().values

    spots = np.linspace(S0 * (1 - SWEEP_MARGIN), S0 * (1 + SWEEP_MARGIN), N_SWEEP)

    dex_s = np.array([
        float((oi * _bs_delta_vec(s, K, T, sigma, tipos) * s * mult).sum())
        for s in spots
    ])

    cruces = np.where(np.diff(np.sign(dex_s)))[0]
    if not len(cruces):
        return None

    i = cruces[np.argmin(np.abs(spots[cruces] - S0))]
    x0, x1, y0, y1 = spots[i], spots[i + 1], dex_s[i], dex_s[i + 1]
    return float(x0 - y0 * (x1 - x0) / (y1 - y0))


# ── Función pública: GEX ──────────────────────────────────────────────────────

def calcular_kpis(csv_path: str, accion: str = None,
                  hoy: date = None) -> dict:
    """
    Calcula KPIs de GEX desde el CSV de meff_opciones.py.

    Args:
        csv_path: ruta al CSV (sep=";", utf-8-sig)
        accion:   nombre del subyacente. Si es None → calcula todos.
        hoy:      fecha de referencia. Si es None → date.today()

    Returns (si accion especificado):
        {
            "accion":         str,
            "spot":           float,
            "call_wall":      float | None,
            "put_wall":       float | None,
            "zero_gamma":     float | None,   # None si GEX no cruza cero en ±15%
            "gex_total":      float,
            "regime":         "POSITIVO" | "NEGATIVO",
            "multiplicador":  int,
            "fecha":          date,
            "gex_por_strike": pd.DataFrame,   # strike|call_gex|put_gex|net_gex
        }

    Returns (si accion=None):
        { accion: kpis_dict, ... }  para todos los subyacentes del CSV
    """
    if hoy is None:
        hoy = date.today()

    df_raw = pd.read_csv(csv_path, sep=";", encoding="utf-8-sig", dtype=str)

    requeridas = {"accion", "tipo", "strike", "posicion_abierta",
                  "volatilidad_cierre", "spot", "fecha_vencimiento"}
    faltantes = requeridas - set(df_raw.columns)
    if faltantes:
        raise ValueError(
            f"El CSV no tiene las columnas necesarias: {sorted(faltantes)}\n"
            f"Asegúrate de correr meff_opciones.py antes de meff_gex.py."
        )

    if accion is not None:
        return _kpis_accion(df_raw, accion, hoy)

    resultado = {}
    for ac in df_raw["accion"].dropna().unique():
        k = _kpis_accion(df_raw, ac, hoy)
        if k is not None:
            resultado[ac] = k
    return resultado


def _kpis_accion(df_raw: pd.DataFrame, accion: str, hoy: date) -> dict | None:
    df = _preparar(df_raw, accion, hoy)
    if df.empty:
        return None

    S0   = float(df["spot_val"].dropna().iloc[0])
    mult = get_multiplicador(accion)

    df  = _calcular_gex_filas(df, S0, mult)
    gs  = _agregar_por_strike(df)

    call_wall = float(gs.loc[gs["call_gex"].idxmax(), "strike"]) if not gs.empty else None
    put_wall  = float(gs.loc[gs["put_gex"].idxmin(),  "strike"]) if not gs.empty else None
    gex_total = float(df["gex"].sum())
    zero_gm   = _calcular_zero_gamma(S0, df, mult)
    regime    = "POSITIVO" if gex_total >= 0 else "NEGATIVO"

    fecha = hoy
    if "fecha_boletin" in df.columns:
        try:
            fb = df["fecha_boletin"].iloc[0]
            fecha = (datetime.strptime(fb, "%d/%m/%y").date()
                     if len(fb) <= 8 else datetime.strptime(fb, "%d/%m/%Y").date())
        except Exception:
            pass

    return {
        "accion":         accion,
        "spot":           S0,
        "call_wall":      call_wall,
        "put_wall":       put_wall,
        "zero_gamma":     zero_gm,
        "gex_total":      gex_total,
        "regime":         regime,
        "multiplicador":  mult,
        "fecha":          fecha,
        "gex_por_strike": gs,
    }


# ── Función pública: DEX ──────────────────────────────────────────────────────

def calcular_kpis_dex(csv_path: str, accion: str = None,
                      hoy: date = None) -> dict:
    """
    Calcula KPIs de DEX desde el CSV de meff_opciones.py.
    Misma interfaz que calcular_kpis() pero usando delta en lugar de gamma.

    Args:
        csv_path: ruta al CSV (sep=";", utf-8-sig)
        accion:   nombre del subyacente. Si es None → calcula todos.
        hoy:      fecha de referencia. Si es None → date.today()

    Returns (si accion especificado):
        {
            "accion":         str,
            "spot":           float,
            "call_wall":      float | None,   # strike con mayor DEX call
            "put_wall":       float | None,   # strike con menor DEX put (más negativo)
            "zero_delta":     float | None,   # None si DEX no cruza cero en ±15%
            "dex_total":      float,
            "regime":         "LONG" | "SHORT",
            "multiplicador":  int,
            "dex_por_strike": pd.DataFrame,   # strike|call_dex|put_dex|net_dex
        }

    Returns (si accion=None):
        { accion: kpis_dict, ... }  para todos los subyacentes del CSV
    """
    if hoy is None:
        hoy = date.today()

    df_raw = pd.read_csv(csv_path, sep=";", encoding="utf-8-sig", dtype=str)

    requeridas = {"accion", "tipo", "strike", "posicion_abierta",
                  "volatilidad_cierre", "spot", "fecha_vencimiento"}
    faltantes = requeridas - set(df_raw.columns)
    if faltantes:
        raise ValueError(
            f"El CSV no tiene las columnas necesarias: {sorted(faltantes)}\n"
            f"Asegúrate de correr meff_opciones.py antes de calcular DEX."
        )

    if accion is not None:
        return _kpis_dex_accion(df_raw, accion, hoy)

    resultado = {}
    for ac in df_raw["accion"].dropna().unique():
        k = _kpis_dex_accion(df_raw, ac, hoy)
        if k is not None:
            resultado[ac] = k
    return resultado


def _kpis_dex_accion(df_raw: pd.DataFrame, accion: str, hoy: date) -> dict | None:
    df = _preparar(df_raw, accion, hoy)
    if df.empty:
        return None

    S0   = float(df["spot_val"].dropna().iloc[0])
    mult = get_multiplicador(accion)

    df  = _calcular_dex_filas(df, S0, mult)
    gs  = _agregar_por_strike_dex(df)

    call_wall = float(gs.loc[gs["call_dex"].idxmax(), "strike"]) if not gs.empty else None
    put_wall  = float(gs.loc[gs["put_dex"].idxmin(),  "strike"]) if not gs.empty else None
    dex_total = float(df["dex"].sum())
    zero_dt   = _calcular_zero_delta(S0, df, mult)
    regime    = "LONG" if dex_total >= 0 else "SHORT"

    return {
        "accion":         accion,
        "spot":           S0,
        "call_wall":      call_wall,
        "put_wall":       put_wall,
        "zero_delta":     zero_dt,
        "dex_total":      dex_total,
        "regime":         regime,
        "multiplicador":  mult,
        "dex_por_strike": gs,
    }
