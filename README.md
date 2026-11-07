# Forex Trader Skill

Diez skills de Claude para operar Forex de forma autónoma con MetaTrader 5:
escanear pares, leer noticias, cronometrar la entrada, planificar la salida,
repetir el trade 10.000 veces antes de arriesgar, calcular lotes, comprobar el
riesgo, ejecutar sin confirmación por orden y llevar un journal de resultados.

El proyecto adapta a Forex el flujo de trading intradía de
[day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill) y se
inspira también en [trader-skill](https://github.com/Jakeschincariol/trader-skill).
Ambos proyectos son de Jake Schincariol. La adaptación trabaja con pares de
divisas, pips, lotes, spread, margen y apalancamiento mediante MetaTrader 5.

> **Experimento, no consejo financiero.** El pack está pensado para una cuenta
> **demo** de 1.000 USD y apalancamiento 1:1000. El apalancamiento alto puede
> liquidar una cuenta muy rápido. Nada de esto es asesoramiento financiero.

---

## Qué cambia respecto al repo original

| Original (acciones) | Aquí (Forex) |
| --- | --- |
| Universo de ~5.900 acciones de Nasdaq Trader | Watchlist de pares en MT5, o todos los símbolos FX del bróker |
| Yahoo Finance, barras de 5 minutos | MetaTrader 5, barras M5 y D1 del terminal conectado |
| Dólares por acción, número de acciones | Lotes, pips, valor del pip, margen y apalancamiento |
| Apertura NYSE 09:30–09:45 ET | Sesión Forex: Londres 03:00 ET por defecto, 30 minutos |
| `max_position_pct` | `max_margin_pct` (margen máximo sobre equity) |
| `max_order_usd` | `max_order_lots` |
| Solo largo en la práctica | **BUY y SELL** en todas las reglas |
| El usuario escribe “yes” por orden | **Ejecución autónoma sin confirmación por orden**; el risk check sigue siendo la puerta |
| Broker Alpaca / paper | Broker MetaTrader 5, demo por defecto |
| `analyst` y `pelosi` | `trader-journal` (rendimiento y auditoría de decisiones) |

Las cinco estrategias diarias se conservan y se les añade el espejo corto:
ruptura/ruptura bajista de 20 días, bandera alcista/bajista, recuperación o
rechazo de la media de 20, sobreventa/sobrecompra y rebote. El ORB de acciones
se traslada a la sesión de Londres (o Asia, solapamiento o Nueva York).

---

## Requisitos

- **Windows** (MetaTrader 5 y el paquete `MetaTrader5` son Windows-only).
- **Python 3.8+**.
- **MetaTrader 5** instalado y con una cuenta **demo** conectada.
- El paquete `MetaTrader5`:

```powershell
pip install MetaTrader5
```

No hace falta ninguna API key ni servicio de pago. Las noticias usan RSS
públicos (ForexLive, FXStreet, DailyFX).

---

## Instalación

```powershell
git clone https://github.com/xcry99/forex-trader-skill.git
cd forex-trader-skill
pip install MetaTrader5
```

Los scripts importan el paquete compartido `fx/` desde la raíz del repo, así
que ejecútalos desde la raíz (o pon la raíz en `PYTHONPATH`).

Si quieres instalarlo como plugin de Claude Code, el repo incluye
`.claude-plugin/marketplace.json` y `.claude-plugin/plugin.json`. Copiar las
carpetas `trader-*` a `~/.claude/skills/` también funciona; en ese caso
mantén `fx/` junto a ellas o instala el paquete con `pip install -e .`.

---

## Configurar MetaTrader 5

1. Abre el terminal de MetaTrader 5 y entra en la cuenta **demo**.
2. Activa **Tools > Options > Expert Advisors > Allow Algo Trading**
   (Herramientas > Opciones > Asesores Expertos > Permitir trading algorítmico).
   Sin esto, `terminal_info().trade_allowed` es `False` y el risk check bloquea
   todas las entradas con ese motivo. Las salidas sí se permiten.
3. Comprueba que los pares de la watchlist son visibles en el Market Watch.
   El gateway los selecciona solo si hace falta.
4. Anota el login, el servidor y el balance. En `trader/config.json` puedes
   dejar `mt5.login`, `password`, `server` y `path` en `null` para usar el
   terminal que ya está abierto y logueado.

Comprobación rápida:

```powershell
python trader-broker/broker.py account
python trader-broker/broker.py positions
```

El pack funciona con cualquier cuenta demo de MT5 compatible con la operativa
automática.

---

## Configuración del experimento

Todo vive en `trader/config.json`. Los valores por defecto ya son el
experimento de 1.000 USD:

```json
{
  "account": {
    "budget": 1000.0,
    "starting_balance": 1000.0,
    "leverage": 1000,
    "mode": "demo",
    "auto_execute": true
  },
  "risk": {
    "max_loss_per_trade_usd": 10.0,
    "max_daily_loss_usd": 30.0,
    "max_trades_per_day": 3,
    "max_orders_per_day": 10,
    "min_reward_risk": 1.0,
    "max_margin_pct": 75,
    "cash_buffer_pct": 10,
    "max_order_lots": 10.0,
    "min_order_lots": 0.01,
    "allow_short": true,
    "require_stop": true,
    "no_entries_before": "02:30",
    "no_entries_after": "11:30",
    "flat_by": "16:55"
  }
}
```

### `budget` no es el balance de la demo

`account.budget` es el capital con el que se dimensiona el experimento:
**1.000 USD**. El balance real de la cuenta demo puede ser 5.000 USD; se usa
solo como capacidad de margen. El sizer toma el menor de los dos límites
(presupuesto y margen libre), así que el riesgo por operación sigue siendo de
10 USD (1 % del presupuesto), no 50 USD.

`account.starting_balance` es la base del retorno y del drawdown del journal.
Si cambias el presupuesto, cambia también `starting_balance` para que las
métricas tengan sentido.

### Riesgo traducido a Forex

- **10 USD por operación**: con un stop de 20 pips en EURUSD son 0,05 lotes
  (0,05 × 20 pips × 10 USD/pip/lote = 10 USD).
- **30 USD de pérdida diaria**: como máximo tres operaciones a pleno riesgo.
- **3 entradas al día** y **10 órdenes al día** en total.
- **75 % de margen máximo** sobre el equity, con un **10 % de colchón** de
  margen libre.
- **Stop obligatorio** y **reward:risk mínimo 1:1**.
- **Ventana de entradas 02:30–11:30 NY**; **flat_by 16:55 NY**.
- **Shorts permitidos** (`allow_short: true`).

No hay guardrails ocultos: las reglas son las del pack original, traducidas a
lotes, pips y margen. Si quieres cambiar una, se cambia en el JSON y se dice.

---

## Flujo completo

```
scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> broker
                                                                       |
                                                                   journal
alerts: stop, target, flat_by                     (mientras hay posición)
```

Cada skill lee lo que escribió el anterior en `trader/`:

| archivo | lo escribe | lo lee |
| --- | --- | --- |
| `universe.json` | scanner | scanner, revisión humana |
| `scan.json` | scanner | entry, broker |
| `news.json` | news | broker (contexto y blackout opcional) |
| `entry.json` | entry | exit |
| `plan.json` | exit | timemachine, budget, alerts, broker |
| `timemachine.json` | timemachine | revisión humana, broker |
| `orders.json` | budget | risk, broker |
| `state/risk.json` | risk | broker |
| `state/broker.json` | broker | risk, journal |
| `journal.jsonl` | journal / broker | journal |
| `decisions.jsonl` | broker | journal |

### Ejemplo de una operación

1. **`trader-scanner`** lee los 14 pares de la watchlist y encuentra que
   EURUSD cerró por encima del rango de apertura de Londres: 1.1020–1.1040,
   con cierre M5 en 1.1045.
2. **`trader-news`** revisa los titulares de EUR y USD. No hay un evento de
   alto impacto en la ventana; el contexto queda registrado.
3. **`trader-entry`** confirma el primer cierre M5 por encima de 1.1040 y
   marca `triggered` a 1.1045.
4. **`trader-exit`** pone el stop en el punto medio del rango, 1.1030 (15
   pips), y el target en 1R: 1.1060.
5. **`trader-timemachine`** repite el resto de la ventana de mantenimiento
   (hasta las 16:55 NY) 10.000 veces con las velas M5 del propio par y
   re-ejecuta la regla en las últimas 60 sesiones. Devuelve probabilidades y
   expectancy; no bloquea.
6. **`trader-budget`** calcula 0,05 lotes para arriesgar 10 USD con 15 pips
   de stop, dentro del margen y del colchón.
7. **`trader-risk`** comprueba pérdida máxima, pérdida diaria, número de
   trades, reward:risk, stop, ventana, spread, margen, duplicados, mercado
   abierto y el interruptor de algo trading. PASS o BLOCK con el motivo.
8. **`trader-broker`** envía solo el PASS, con SL y TP adjuntos en la orden.
   MetaTrader guarda el bracket en el servidor.
9. **`trader-alerts`** vigila el stop, el target y la hora de cierre.
10. **`trader-journal`** sincroniza los deals cerrados y calcula win rate,
    profit factor, expectancy en R, drawdown y el desglose por setup.

---

## Comandos

### Scanner

```powershell
python trader-scanner/scan.py universe
python trader-scanner/scan.py universe --all --live-spreads
python trader-scanner/scan.py daily
python trader-scanner/scan.py orb --session london --or-minutes 30
python trader-scanner/scan.py all
```

Sesiones: `asia` (19:00–04:00 NY), `london` (03:00–12:00), `overlap`
(08:00–12:00), `newyork` (08:00–17:00).

### Noticias

```powershell
python trader-news/news.py fetch --hours 24 --write
python trader-news/news.py fetch --currencies USD,EUR
python trader-news/news.py check --currency USD
```

### Entrada

```powershell
python trader-entry/entry.py check EURUSD --setup orb --session london
python trader-entry/entry.py check GBPUSD --side sell
python trader-entry/entry.py scan --write
python trader-entry/entry.py watch EURUSD --every 30
```

### Salida

```powershell
python trader-exit/exit.py plan --from-entry --stop or-mid --target 1R --write
python trader-exit/exit.py plan --symbol EURUSD --side buy --stop atr:1.5 --target 2R --write
python trader-exit/exit.py show
```

Stops: `or-mid`, `or-low`, `or-high`, `price:X`, `pips:X`, `pct:X`, `atr:K`.
Targets: `1R`, `1.5R`, `2R`, ... o `price:X`.

### Time Machine

```powershell
python trader-timemachine/timemachine.py run --write
python trader-timemachine/timemachine.py run --replays 20000 --seed 7
```

### Tamaño de posición

```powershell
python trader-budget/size.py size --write
python trader-budget/size.py size --budget 1000
python trader-budget/size.py limits
```

### Riesgo

```powershell
python trader-risk/check.py
python trader-risk/check.py --json
```

### Broker autónomo

```powershell
python trader-broker/broker.py account
python trader-broker/broker.py positions
python trader-broker/broker.py status
python trader-broker/broker.py show
python trader-broker/broker.py execute --dry-run
python trader-broker/broker.py auto --once --dry-run
python trader-broker/broker.py auto --once
python trader-broker/broker.py auto --loop --interval 300
python trader-broker/broker.py close EURUSD
python trader-broker/broker.py flatten
python trader-broker/broker.py sync
```

- `auto --once` ejecuta un ciclo completo sin preguntar.
- `auto --loop --interval 300` repite cada cinco minutos.
- `auto --news` refresca los RSS antes de escanear; sin el flag usa el
  `news.json` cacheado.
- `--dry-run` recorre todo el pipeline y dice cuántas órdenes se enviarían.
- `--now "2026-11-04 09:00"` fija el reloj (hora de Nueva York) para revisar un
  ciclo de un día pasado con datos reales; solo vale con `--once`, porque un
  `--loop` necesita el reloj real.
- `--allow-live` es obligatorio en una cuenta real; en demo no hace falta.

### Alertas

```powershell
python trader-alerts/alerts.py add --from-plan
python trader-alerts/alerts.py check
python trader-alerts/alerts.py check --execute
python trader-alerts/alerts.py list
python trader-alerts/alerts.py quote EURUSD USDJPY
```

### Journal

```powershell
python trader-journal/journal.py sync
python trader-journal/journal.py report
python trader-journal/journal.py report --write --json
python trader-journal/journal.py trades --limit 20
python trader-journal/journal.py decisions --limit 30
python trader-journal/journal.py export --csv trader/trades.csv
```

---

## Cómo decide el modo autónomo

`broker.py auto` ejecuta, en orden:

1. Lee cuenta, equity, margen libre y posiciones.
2. Si hay una posición abierta y ya es `flat_by` (16:55 NY), la cierra.
3. Restaura SL/TP si una posición los perdió.
4. Comprueba que el mercado Forex esté abierto (domingo 17:00 ET a viernes
   17:00 ET, con festivos).
5. Escanea los pares (setups diarios + ORB de la sesión) y lee las noticias
   cacheadas; `--news` refresca los RSS antes de escanear.
6. Evalúa la entrada: ORB espera el primer cierre M5 fuera del rango; un setup
   diario entra a mercado.
7. Construye el plan: stop en el punto medio del rango (ORB) o 1,5 × ATR
   diario (setup diario), target 1R. Si el stop del rango queda demasiado
   cerca, cae a 1,5 × ATR de M5.
8. Ejecuta el Time Machine y guarda sus probabilidades en el plan.
9. Calcula lotes con el presupuesto, la pérdida máxima, el margen y el
   colchón.
10. Ordena los planes por expectancy del Time Machine y luego por score.
11. Pasa el lote completo por el risk check y guarda los fingerprints.
12. Envía solo los PASS, revalidando el precio vivo (si el target ya se
    alcanzó o el stop ya se tocó, no persigue).
13. Sincroniza los trades cerrados al journal.

El **risk check es la única puerta dura**. El Time Machine ordena y se
registra, pero no veta. Las noticias son contexto; solo bloquean si el usuario
pone `avoid_high_impact_minutes` por encima de 0.

Cada ciclo, candidato, bloqueo, ejecución y cierre se escribe en
`trader/decisions.jsonl`. Ese archivo es la auditoría del experimento: qué vio
el agente, qué eligió y por qué.

---

## Journal y rendimiento

El journal se construye desde el historial de deals de MetaTrader, no desde la
memoria del agente. Cada posición cerrada se empareja con su plan por ticket y
produce:

- pips reales, bruto, comisión, swap y neto;
- `risk_usd` del plan y `r_multiple` (neto / riesgo);
- setup, sesión, confianza y odds del Time Machine;
- win rate, profit factor, expectancy, avg win/loss, payoff, avg R, drawdown,
  Sharpe por trade, rachas y curva de equity;
- desglose por símbolo, setup y sesión;
- conteo de decisiones: ciclos, candidatos, ejecutadas, bloqueadas, saltadas y
  los motivos de bloqueo por regla.

Un target alcanzado se parece a +1R y un stop a −1R, antes de comisiones y
slippage. El retorno y el drawdown usan `starting_balance` (1.000 USD), no el
balance real de la demo.

---

## Tests

La suite es offline: usa `FakeGateway` y barras sintéticas, no necesita
terminal ni red.

```powershell
python -m unittest discover -s tests -v
```

Cubre pips EURUSD/USDJPY, planes largos y cortos, sizing de 0,05 lotes con 10
USD de riesgo, rechazo cuando el lote mínimo arriesga demasiado, PASS/BLOCK del
risk check, salidas no bloqueadas por reglas de entrada, RSI y setups,
opening range, entrada larga y corta, determinismo del Time Machine,
sincronización y métricas del journal, y alertas de stop/target/tiempo.

---

## Advertencias

- **El apalancamiento 1:1000 es brutal.** Un movimiento pequeño en contra
  puede consumir el margen. El stop de 10 USD por operación es la única cosa
  que mantiene el experimento dentro de un presupuesto de 1.000 USD.
- **Un stop es un plan, no una garantía.** En un gap o en un mercado rápido
  puede llenarse bastante peor que su precio. La pérdida mostrada es la del
  stop, no la máxima posible.
- **El spread y el slippage son reales.** El sizer mide el riesgo desde el ask
  o el bid vivo cuando es peor que el plan, pero no puede garantizar el fill.
- **El Time Machine es una simulación.** Sale de las últimas 60 sesiones del
  par. Un banco central, una elección o un gap de fin de semana pueden hacer
  algo que esas sesiones nunca hicieron.
- **Las noticias son contexto, no una señal.** El etiquetado es por palabras
  clave y los RSS son gratuitos, no oficiales.
- **El mercado Forex está cerrado los fines de semana.** El agente lo detecta
  y no entra; el journal se sincroniza igual.
- **Cuenta demo primero.** En una cuenta real el broker exige
  `--allow-live`. No lo uses hasta que el experimento en demo tenga un
  historial que puedas leer.
- **Nada de esto es asesoramiento financiero.** Es software que lee datos
  públicos y hace aritmética. No conoce tu situación y no ve el futuro. La
  mayoría de los traders retail pierde dinero.

---

## Estructura del proyecto

```
fx/                    núcleo compartido (MT5, pips, sizing, riesgo, journal, tiempo)
  gateway.py           integración MetaTrader 5: ticks, barras, órdenes, posiciones
  fake.py              gateway de test, sin terminal
  symbols.py           contrato, pip, redondeo de precio y volumen
  timeutil.py          UTC, Nueva York, hora del servidor, sesiones, festivos
  indicators.py        RSI, ATR, SMA, setups diarios, opening range
  plan.py              stops y targets largos/cortos
  sizing.py            lotes, margen, valor del pip
  risk.py              reglas de riesgo y fingerprints
  journal.py           deals -> trades, métricas, CSV
  store.py             archivos JSON/JSONL atómicos en trader/
  config.py            defaults y merge de config.json
  cli.py               argparse, tablas, gateway
trader-scanner/        SKILL.md, scan.py
trader-news/           SKILL.md, news.py
trader-entry/          SKILL.md, entry.py
trader-exit/           SKILL.md, exit.py
trader-timemachine/    SKILL.md, timemachine.py
trader-budget/         SKILL.md, size.py
trader-risk/           SKILL.md, check.py
trader-broker/         SKILL.md, broker.py
trader-alerts/         SKILL.md, alerts.py
trader-journal/        SKILL.md, journal.py
trader/                config.json y archivos de runtime
tests/                 unittest offline + FakeGateway
```

`trader/state/`, `journal.jsonl`, `decisions.jsonl`, `scan.json`, `plan.json`,
`orders.json` y compañía están en `.gitignore`: son datos de runtime, no
código.

---

## Créditos y licencia

Gracias a [Jake Schincariol](https://opusjake.ai) por los dos proyectos de referencia.

- [day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill) es la base del flujo intradía que se adaptó a Forex y MetaTrader 5.
- [trader-skill](https://github.com/Jakeschincariol/trader-skill) es el proyecto original que también inspiró esta adaptación.

El proyecto se distribuye bajo la [licencia MIT](LICENSE).
