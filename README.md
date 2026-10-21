# GEX DAX · γ

**Gamma Exposure (GEX) y Delta Exposure (DEX) de las opciones sobre el DAX (Eurex · ODAX), actualizado cada día.**

Dashboard estático (un solo `index.html`) que se alimenta de los JSON de `data/`, regenerados cada mañana por GitHub Actions con la última sesión publicada por Eurex.

---

## Qué muestra

- **γ GEX** — gamma neta de los dealers por strike, para todos los vencimientos o uno concreto.
  KPIs: spot, GEX neto, GEX de calls y puts, call wall, put wall, zero gamma y régimen.
- **Δ DEX** — el mismo análisis con delta: zero delta y sesgo direccional.
- **Glosario** — explicación de cada concepto.

Al elegir un vencimiento, call wall, put wall y zero gamma se recalculan **solo con ese vencimiento**.
El gráfico y la tabla muestran los strikes a ±12 % del DAX; los totales usan todos los strikes.

---

## De dónde salen los datos

Eurex publica gratis, por strike y vencimiento, la **posición abierta**, el **volumen** y el **precio de liquidación** de las opciones ODAX, a través de la API JSON que usa su propia web (pestaña *Prices/Quotes*):

```
https://www.eurex.com/api/v1/overallstatistics/70044?busdate=YYYYMMDD&filtertype=overview
https://www.eurex.com/api/v1/overallstatistics/70044?filtertype=detail&productdate=<vcto>&busdate=<sesión>&contracttype=<W|M>
```

Eurex **no publica ni la volatilidad implícita ni el nivel del DAX**, así que el script los calcula:

| Dato | Cómo se obtiene |
|---|---|
| Forward de cada vencimiento | Paridad put-call: `F = K + (C − P)·e^(rT)`, mediana de los 5 strikes más cercanos al ATM |
| DAX (spot) | `F · e^(−rT)` de los 3 vencimientos más cortos (el DAX es índice de rentabilidad total, sin dividendos) |
| Volatilidad implícita | Inversión de Black-76 con la opción fuera de dinero de cada strike; interpolada entre strikes |
| Gamma y delta | Black-Scholes con esa volatilidad (`gex_calculator.py`) |

Validación con la sesión del 20/10/2026: DAX implícito 25.317 frente a un cierre real de 25.304 (0,05 %); revalorando con la IV calculada, error mediano de 0,04 puntos frente al settlement de Eurex.

---

## Metodología

- **Multiplicador:** 5 € por punto (ODAX)
- **GEX:** `OI_call × Γ × S² × 5 − OI_put × Γ × S² × 5` (convención SpotGamma: dealers cortos en ambos lados)
- **DEX:** `OI_call × Δ_call × S × 5 + OI_put × Δ_put × S × 5`
- **Vencimientos:** semanales y mensuales (los diarios ODAP no están incluidos)
- **Tiempo a vencimiento:** días naturales desde la sesión / 365
- **Tasa libre de riesgo:** 2,5 % (constante `R` en `gex_calculator.py`)
- **Zero gamma / zero delta:** barrido de 600 precios en ±15 % del spot

---

## Estructura

```
├── index.html                 ← dashboard (GEX · DEX · Glosario), sin build
├── eurex_dax.py               ← descarga Eurex + forward + IV + GEX/DEX → data/
├── gex_calculator.py          ← librería Black-Scholes vectorizada (gamma, delta, zero gamma)
├── requirements.txt
├── .github/workflows/
│   └── dax_daily.yml          ← ejecución diaria (martes a sábado, 06:17 UTC)
├── ejemplos/
│   └── eurex_odax_20261020_raw.csv   ← descarga en bruto para probar sin red
└── data/
    ├── dax_gex_latest.json           ← GEX + DEX (pestañas γ y Δ)
    ├── dax_opciones_latest.json      ← IV y OI por strike (vista tabla)
    ├── dax_gex_YYYYMMDD.json         ← histórico (últimas 60 sesiones)
    └── eurex_odax_YYYYMMDD.csv       ← datos normalizados con forward e IV
```

---

## Uso en local

```bash
pip install -r requirements.txt

python eurex_dax.py                       # última sesión publicada por Eurex
python eurex_dax.py --busdate 20261016    # una sesión concreta (Eurex guarda ~20)
python eurex_dax.py --from-csv ejemplos/eurex_odax_20261020_raw.csv --busdate 20261020   # sin red

python -m http.server 8080                # y abre http://localhost:8080/
```

`index.html` no se puede abrir con doble clic (`file://`): el navegador bloquea la carga de los JSON. Hay que servirlo con el comando anterior o desde GitHub Pages.

---

## Aviso

Herramienta de análisis del posicionamiento en opciones, no una recomendación de inversión. Los datos son propiedad de Eurex; antes de publicar este dashboard en abierto, revisa sus condiciones de uso de datos de mercado.
