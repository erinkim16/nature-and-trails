# Nature & Trails

An autonomous trip-planning agent for places the travel industry handles badly:
national parks, long-distance hiking routes, and alpine hut-to-hut traverses.

Give it a destination, a month, a budget and your fitness level. It searches real
trail data, mountain huts, weather history and terrain, then produces a validated
day-by-day itinerary with a packing list, a budget, and an honest list of what it
could not verify.

```bash
trails plan "6 days hut-to-hut in the Dolomites in September, moderate fitness, budget 1200 EUR, I like via ferrata and big views"
```

---

## Why this is different from every other AI travel planner

Mainstream travel tools are thin wrappers over hotel and flight inventory. Those
APIs know nothing about a 120 km traverse at 2,500 m. This project is built on the
data that actually describes wilderness travel:

| It knows about                                                                    | Because it reads                       |
| --------------------------------------------------------------------------------- | -------------------------------------- |
| The Alta Via 1, Tour du Mont Blanc, GR20 — with length, difficulty and waymarking | OpenStreetMap `route=hiking` relations |
| Alpine huts with elevation, bed capacity, operator and phone number               | OSM `tourism=alpine_hut`               |
| Whether a cable car can remove 1,200 m of ascent from your day                    | OSM `aerialway`                        |
| Where the drinking water is on a dry limestone massif                             | OSM `amenity=drinking_water`           |
| That September at 2,500 m averages 1.1 °C overnight with 16 wet days              | Open-Meteo reanalysis archive          |
| Whether the freezing level sits below your route's high point                     | Open-Meteo `freezing_level_height`     |

Every one of those sources is free and needs no API key.

A real example of what that buys you — this is live output from the smoke test:

```
2. find_hiking_routes(Dolomites bbox, min 30 km)
  [ok] 12 long routes found; top 5:
       - Traumpfad München-Venedig                                   550.0 km  [iwn]
       - Sentiero della Pace                                         520.0 km  [nwn]
       - Alta Via n. 2 delle Dolomiti - Dolomiten-Höhenweg Nr. 2     185.0 km  [rwn]
       - Alta via n. 6 delle Dolomiti                                180.0 km  [rwn]
       - Alta via n. 9 delle Dolomiti - Dolomiten-Höhenweg Nr. 9     140.0 km  [rwn]

3. find_mountain_huts(same bbox)
  [ok] 200 huts found; highest 5:
       - Capanna Punta Penia                                3340 m
       - Rifugio Capanna Piz Fassa                          3152 m
       - Rifugio Maria Vittoria Torrani                     2984 m
```

---

## Quickstart

```bash
cd backend
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"     # Windows
# source .venv/bin/activate && pip install -e ".[dev]"  # macOS / Linux
```

Then add your key:

```bash
cp .env.example .env
```

Put an Anthropic API key in `.env` (get one at
<https://console.anthropic.com/settings/keys>) and set `NT_CONTACT_EMAIL` to a real
address — OpenStreetMap's usage policy requires an identifying `User-Agent`, and
you will eventually be blocked without one.

Verify the data layer works before spending any tokens — this needs **no API key**:

```bash
.venv/Scripts/python.exe scripts/smoke_tools.py
```

Then plan a trip:

```bash
.venv/Scripts/python.exe -m nature_trails.cli plan "5 days in Torres del Paine in November, first big trek, budget 900 USD" --out ../out
```

### Commands

| Command                                  | What it does                                                                                                           |
| ---------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `trails plan "<request>"`                | Plan a trip. `--out DIR` saves Markdown + JSON. `--quiet` hides the model's reasoning. `--no-intake` skips the intake. |
| `trails tools`                           | List the tools the agent can call and what each is for.                                                                |
| `uvicorn nature_trails.api:app --reload` | Run the HTTP API. Interactive docs at <http://localhost:8000/docs>.                                                    |
| `python scripts/smoke_tools.py`          | Exercise every data source live. No API key needed.                                                                    |
| `pytest`                                 | 95 tests. No API key or network needed.                                                                                |

(`trails` is installed as a console script by `pip install -e .`; the
`python -m nature_trails.cli` form works identically.)

---

## How it works

An intake, then two phases. The phase split is the central design decision — see
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for why.

**Intake (no API call).** Five questions the agent cannot look up and always needs:
passport country, where you'd rather sleep, group size, comfort with exposure, and
how fixed your dates are. Press Enter to accept any default. Nationality matters
more than it looks — an American needs an America the Beautiful pass and
Recreation.gov lotteries, while an EU citizen gets 30–50% hut discounts _and_
helicopter-rescue cover through alpine club reciprocity. Skip it with `--no-intake`;
it's skipped automatically when stdin isn't a terminal, so scripted runs never stall.

This is the traveller's only channel into the agent — there is deliberately no
mid-run questioning. A suspended agent waiting on a human is free in a terminal
and a distributed-systems problem on the web, so the agent is told it cannot ask and
must state its assumptions instead. Reasoning in
[ARCHITECTURE.md](docs/ARCHITECTURE.md) 9.

**Phase 1 — Research (an agent).** A tool-calling loop. Claude decides which of the
12 tools to call and in what order, runs them in parallel, reads the results, and
keeps going until it has enough. Ends with a plain-prose research brief.

**Phase 2 — Assembly (not an agent).** Two constrained API calls with no tools,
where each response is forced to match a Pydantic schema. They run concurrently and
merge into a validated `Itinerary` — guaranteed to have every field, every day,
every number. (Two rather than one because the combined schema exceeds the
structured-output grammar limit; see ARCHITECTURE 5.1.)

That guarantee is why the renderers are simple, and why the planned Next.js
frontend can consume the same object without a parser.

```
cli.py  ──►  agent/  ──►  tools/  ──►  core/
(or FastAPI)   loop      12 tools    http cache,
               prompts   registry    rate limits,
               schemas               geo maths
```

Dependencies point one way only. `core` never imports `tools`; `tools` never
imports `agent`. That is what keeps the agent UI-agnostic.

### The tools

| Tool                    | Answers                                                     |
| ----------------------- | ----------------------------------------------------------- |
| `geocode_place`         | Where exactly is this?                                      |
| `make_bounding_box`     | Build a search area of radius N around a point              |
| `find_hiking_routes`    | What multi-day routes exist here?                           |
| `get_route_geometry`    | Give me the actual line, so I can measure it                |
| `find_mountain_huts`    | Where can I sleep up high?                                  |
| `find_trail_amenities`  | Water, peaks, passes, via ferrata, cable cars               |
| `find_trailhead_access` | Can I get there without a car?                              |
| `get_elevation`         | Real ascent, with elevation-model noise filtered out        |
| `get_weather_forecast`  | Actual forecast (only useful within 16 days)                |
| `get_climate_normals`   | What is September usually like at 2,500 m?                  |
| `get_daylight`          | Do I have enough light for an 8-hour stage?                 |
| `build_packing_list`    | Deterministic gear rules engine                             |
| `web_search`            | Permits, closures, hut opening dates, flights (server-side) |

---

## Cost

Runs on `claude-opus-5` with adaptive thinking. **A measured full run** (6-day
Dolomites hut trip) came out at:

```
11 turns · 22 tool calls · 13 API requests
tokens: 227,071 in / 31,188 out · cache 340,005 read, 47,188 written
estimated cost: $2.380
```

So budget **roughly $2–3 per plan**, not cents. The CLI prints the actual numbers
after every run, plus a warning if prompt caching stopped working. If that is too
rich while you iterate, the cheapest levers in order: set `NT_RESEARCH_EFFORT=medium`,
lower `NT_MAX_TURNS`, or set `NT_MODEL=claude-sonnet-5` (about 40% of the input cost
and 60% of the output cost of Opus).

Two things keep the bill down:

- **Prompt caching.** Tools and the system prompt are byte-stable and sit first in
  the request, so they cache across turns. Everything volatile lives in `messages`.
- **`NT_MAX_TURNS`** (default 30) is a hard ceiling on the loop, so a confused
  agent cannot spin indefinitely.

The OSM and Open-Meteo calls are free, and cached in SQLite — re-planning the same
region costs nothing and returns instantly.

---

## HTTP API

A plan takes minutes, so the API is job-based rather than request-response. Start the
server with `uvicorn nature_trails.api:app --reload` and open `/docs`.

| Endpoint                 | Purpose                                                    |
| ------------------------ | ---------------------------------------------------------- |
| `POST /plans`            | Start a plan. Returns `202` with a job id **immediately**. |
| `GET /plans/{id}`        | Poll state; carries the validated `Itinerary` once `done`. |
| `GET /plans/{id}/events` | Server-Sent Events: live tool calls, turns, cost.          |
| `DELETE /plans/{id}`     | Cancel a run you no longer want to pay for.                |
| `GET /health`            | Liveness probe, for Railway and friends.                   |
| `GET /openapi.json`      | Schema — feeds `openapi-typescript` for the frontend.      |

Events are **replayable**: a client that connects after `POST` (which every browser
does) receives the whole backlog before joining the live feed, so nothing is missed
and `EventSource` reconnection is harmless.

Three ceilings, because they bound different things: `NT_MAX_TURNS` bounds turns,
`NT_JOB_TIMEOUT_SECONDS` bounds wall-clock, `NT_MAX_CONCURRENT_PLANS` bounds how many
run at once.

A measured run through the API:

```
POST /plans          -> 202 in ~20 ms
state done after     568 s
15 API requests · 251,566 in / 32,108 out · 511,889 cache reads
estimated cost       $2.55
```

**Deploying the backend:** a `Procfile` and `.python-version` are included, so
Railway (or anything Procfile-aware) works out of the box. Set `ANTHROPIC_API_KEY`,
`NT_CONTACT_EMAIL`, and `NT_CORS_ORIGINS` (your Vercel domain) as host env vars.
Do **not** try to host this on Vercel functions — they cap execution at seconds.

---

## Project layout

```
Nature&Trails/
├── backend/               Python: agent, tools, CLI (and later FastAPI)
│   ├── src/nature_trails/
│   │   ├── core/          config, cached HTTP client, geo maths
│   │   ├── tools/         the 12 tools + registry
│   │   ├── agent/         loop, prompts, intake, itinerary schema
│   │   ├── render/        terminal + Markdown output
│   │   ├── cli.py         terminal entry point
│   │   └── api.py         HTTP API (jobs + SSE)
│   ├── scripts/           live smoke test
│   └── tests/             95 tests, no network required
├── web/                   Next.js frontend (not built yet)
└── docs/
    ├── ARCHITECTURE.md    design reasoning + what broke on first live contact
    └── WEB.md              FastAPI + Next.js roadmap
```

---

## Status and roadmap

**Working now:** all 12 tools verified against live data; the two-phase agent; a CLI
with a live streaming view; an HTTP API with jobs and Server-Sent Events; Markdown
and JSON export; 95 tests; lint and format clean. Both the CLI and the API are
verified end-to-end against the live Anthropic API.

**Next:**

1. **FastAPI layer** — wrap `TripPlanner` in an HTTP endpoint that streams progress
   as server-sent events. Subclass `Observer` to push events; the agent does not
   change.
2. **Next.js frontend** — generate TypeScript types from the FastAPI OpenAPI spec
   so the `Itinerary` shape stays in sync across Python and TypeScript from one
   definition.
3. **Map view** — the tools already return coordinates for every hut, route and
   amenity; nothing renders them yet.
4. **GPX export** — `get_route_geometry` already fetches the line.

---

## Development notes

Built with AI assistance (Claude Code). The architecture decisions and data-source
selection are documented so they can be reviewed rather than taken on trust.

---

## A caution

This produces a _plan_, not a guarantee. Mountain conditions change, huts close,
passes hold snow later than any average suggests. The itinerary's `unknowns`
section exists precisely because the agent is required to say what it could not
verify — read it, and confirm anything safety-critical with the local alpine club,
park service or hut operator before you commit.
