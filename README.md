# rebalance

Calculating the cost-service trade-off of overnight rebalancing.

> Independent student project, not affiliated with or endorsed by Transport for London.

## Status

Initial set-up.

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```powershell
git clone <repo-url>
cd <folder>
uv sync --locked
Copy-Item .env.example .env   # then add your TfL API key
```

Before committing, run (in this order):

```powershell
uv run ruff format
uv run ruff check --fix
uv run ty check
uv run pytest
```

## Data

No data is included in this repository. Live-feed and trip data come from
Transport for London under its
[transport data licence](https://tfl.gov.uk/corporate/terms-and-conditions/transport-data-service).

## Attribution

Powered by TfL Open Data

Contains:

- OS data © Crown copyright and database rights 2016
- Geomni UK Map data © and database rights [2019]

## Project structure
