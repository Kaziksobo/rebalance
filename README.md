# rebalance

**Where Should the Bikes Go? Forecast-Driven Overnight Bike Repositioning for London's Santander Cycles**

An open, reproducible evaluation of the cost-service trade-off of overnight bike rebalancing, built on public TfL data.

> Independent student project, not affiliated with or endorsed by Transport for London.

Final-year project for the BSc Data Science and Computing at Birkbeck, University of London (2026/27).

## Overview

Docked bike-share schemes drift out of balance: commuters empty some stations and fill others, so riders arrive to find no bike or no free dock. Operators fix this by moving bikes between stations in vans, which costs money. TfL's own rebalancing methods are not public, so this project does not try to beat them. It asks a question that public data can answer:

> How much of observed demand can overnight rebalancing serve at a given daily cost, and at what cost does additional spend stop meaningfully improving service?

The answer is a cost-service curve rather than a single "optimal" plan, so anyone can read off the best choice for whatever exchange rate between cost and service they prefer. Every claim is relative to stated model assumptions and transparent baselines. Nothing here is a statement about TfL's actual spend or fleet.

## Approach

1. **Reconstruct** each station's bike inventory and each bike's movements from the public trip files.
2. **Forecast** next-day station demand with a ladder of three forecasts: perfect hindsight (ceiling), same weekday last week (floor), and a learned model trained on development days only.
3. **Plan** overnight van moves (00:00 to 05:00) that steer stations towards forecast-driven targets, under van capacity, travel time and cost constraints.
4. **Replay** the following day's observed trips through an event-driven simulator. A trip counts as served if the start station had a bike and the end station had a free dock.
5. **Evaluate** on held-out days, sweeping the number of vans to trace the cost-service curve, with sensitivity runs on the cost and operating assumptions.

A 24-hour reactive regime and rider-behaviour extensions (such as walking to a nearby station) are planned as secondary experiments, subject to time.

The headline measure is the share of *observed* trips served. Failed trips leave no record, so demand that never became a trip is not counted, and results should be read with that censoring in mind.

## Status

| Component | State |
|---|---|
| Live-feed logger (`src/rebalance/collect.py`) | Built, tested and deployed on a Raspberry Pi; collecting since 8 November 2026. 24-hour acceptance run still to complete |
| Log backup and checking scripts | Designed, not yet written |
| Trip-data manifest, ingestion and cleaning | Next |
| Inventory and bike-chain reconstruction | Planned |
| Forecast ladder (including a PyTorch model) | Planned |
| Simulator, planner and cost model | Planned |
| Evaluation, sensitivity analysis and report | Planned |

## Repository layout

| Path | Purpose |
|---|---|
| `src/rebalance/` | The package. `collect.py` is the live-feed logger |
| `tests/` | Test suite (pytest) |
| `scripts/` | One-off checks and operations tooling, never imported by the package |
| `config/` | Cost and operating parameters, kept out of the code so results are easy to reproduce and vary |
| `data/`, `outputs/` | Raw data and generated results (not tracked) |

## Getting started

Requires [uv](https://docs.astral.sh/uv/) (0.13 or later). Python 3.13 is pinned in `.python-version` and uv will fetch it if needed.

```powershell
git clone https://github.com/Kaziksobo/rebalance.git
cd rebalance
uv sync --locked
Copy-Item .env.example .env   # then add your TfL API key
```

A free key is available from the [TfL API portal](https://api-portal.tfl.gov.uk). `.env` is git-ignored: never commit a real key.

### Live-feed logger

The logger polls the TfL BikePoint feed once a minute and appends each raw response to a gzipped JSON-lines file per UTC day under `data/live/`. It uses only the standard library so that it can run on its own on a small always-on machine.

```powershell
uv run python src/rebalance/collect.py --once   # one poll, then exit
uv run python src/rebalance/collect.py          # poll continuously
```

Design points:

- Payloads are stored untouched and timestamped by the logger in UTC, so nothing is lost to decisions made at collection time.
- Failed polls are recorded as explicit failure records, so coverage and gaps can be measured later.
- The API key travels in the request URL, so URLs and exception text are never logged or stored.
- A second instance is refused, and the logger stops loudly if the clock is implausible or the disk is full.

## Development

Before committing, run in this order:

```powershell
uv run ruff format
uv run ruff check --fix
uv run ty check
uv run pytest
```

Tooling: [uv](https://docs.astral.sh/uv/) for environments, [Ruff](https://docs.astral.sh/ruff/) for linting and formatting, [ty](https://docs.astral.sh/ty/) for type checking, and [pytest](https://docs.pytest.org/) with branch coverage and warnings treated as errors. Dependencies are added only when first needed, and `uv.lock` is committed.

## Data

No data is included in this repository. Live-feed and trip data come from Transport for London under its [transport data licence](https://tfl.gov.uk/corporate/terms-and-conditions/transport-data-service). The trip data is public and anonymised, and contains no rider identifiers. This project collects no data from human participants.

## Licence

Code is released under the [MIT licence](LICENSE). The licence covers the code only, not TfL data.

## Attribution

Powered by TfL Open Data

Contains:

- OS data © Crown copyright and database rights 2016
- Geomni UK Map data © and database rights [2019]

## Author

Kazik Sobotnicki. BSc Data Science and Computing, Birkbeck, University of London.
