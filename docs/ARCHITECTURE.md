# Nature & Trails — Architecture

> A trip-planning agent for wilderness destinations: national parks, long-distance
> hikes, alpine traverses. Not a hotel booking bot.

---

## 1. Why this is an _agent_ and not a script

There is a hierarchy of LLM application designs. Picking the right tier is the first
real engineering decision, and most people over-reach.

| Tier            | What it is                                                                  | Example                            |
| --------------- | --------------------------------------------------------------------------- | ---------------------------------- |
| **Single call** | One prompt in, one answer out                                               | "Summarize this trail description" |
| **Workflow**    | The application owns the control flow; the LLM fills in steps               | geocode -> fetch weather -> format |
| **Agent**       | _The model_ decides which tools to call, in what order, and when it is done | this project                       |

The four questions that justify the agent tier:

1. **Complexity** — is the task multi-step and impossible to fully specify up front?
   _Yes._ Planning the Alta Via 1 requires different tool calls than planning
   Yosemite day-hikes. The sequence cannot be hardcoded: the agent must discover
   that a route is hut-based, then look up huts, then check whether they are open in
   the requested month, then re-plan if a pass is snowbound.
2. **Value** — does the outcome justify the cost and latency? _Yes_ — this replaces
   hours of forum-trawling.
3. **Viability** — is Claude good at this? _Yes_, given real data via tools.
4. **Cost of error** — recoverable? _Yes_, the itinerary is reviewed before booking.

---

## 2. The tool surface is the product

> An agent is only as good as its tools. The prompt is maybe 20% of the quality;
> the tool surface is the other 80%.

The classic beginner mistake is one giant `search_the_web(query)` tool. The model
then has to do all the work in natural language and returns vague, uncitable results.

Instead every tool maps to a **noun a mountain guide would recognise**:

| Tool                    | Answers                                          | Source                          |
| ----------------------- | ------------------------------------------------ | ------------------------------- |
| `geocode_place`         | "Where exactly is Cortina d'Ampezzo?"            | Nominatim (OSM)                 |
| `find_hiking_routes`    | "What multi-day routes exist near here?"         | Overpass (OSM relations)        |
| `find_mountain_huts`    | "Where can I sleep up high?"                     | Overpass (`tourism=alpine_hut`) |
| `find_trail_amenities`  | "Water? Cable cars? Passes? Peaks?"              | Overpass                        |
| `find_trailhead_access` | "How do I physically get to the start?"          | Overpass (parking, bus, lifts)  |
| `get_weather_forecast`  | "What is the weather in the next 16 days?"       | Open-Meteo                      |
| `get_climate_normals`   | "What is July _usually_ like at 2500 m?"         | Open-Meteo Archive              |
| `get_route_geometry`    | "Give me the real line, so it can be measured"   | Overpass (`out geom`)           |
| `get_elevation`         | "How much climbing is that really?"              | Open-Meteo Elevation            |
| `make_bounding_box`     | "Build a search area of radius N"                | computed                        |
| `get_daylight`          | "Do I have enough light for 8 hours of walking?" | computed (NOAA algorithm)       |
| `build_packing_list`    | "What do I bring?"                               | **deterministic rules engine**  |
| `web_search`            | permits, closures, current conditions, flights   | Anthropic server-side tool      |

### Design principle: push determinism into tools, leave judgment to the model

`build_packing_list` is a _rules engine_, not an LLM guess. Inputs (max elevation,
month, hut-vs-tent, forecast low temperature, water availability, duration)
deterministically produce a base list. That makes it **testable** and **consistent**
— the same trip always yields the same core gear. The model then adds judgment on
top ("you said you sleep cold, take the warmer bag").

The general principle: whatever can be computed is computed. Models are reserved
for the ambiguous parts.

---

## 3. Caching is not optional

Overpass is a free, community-run, heavily rate-limited service. Nominatim will
**block the calling IP** for exceeding 1 request/second or omitting a `User-Agent`.
Development repeats the same query hundreds of times.

So `core/http.py` provides:

- **SQLite response cache** keyed by `sha256(method + url + sorted params + body)`,
  with a per-source TTL (trails: 30 days — they do not move; weather: 1 hour).
- **Token-bucket rate limiter** per host.
- **Retry with exponential backoff + jitter** on 429/502/504 (Overpass loves 504).

Effect: a second run over the same region is near-instant and free.

---

## 4. The agentic loop

```
                 +--------------------------------------+
                 |  messages = [user request]           |
                 +---------------+----------------------+
                                 v
        +------------------------------------------------+
        |  POST /v1/messages  (streaming)                |
        |    model, system, tools, messages              |
        +---------------+--------------------------------+
                        v
              stop_reason == "tool_use" ?
                 |                    |
                yes                   no
                 |                    +--> done, return final message
                 v
     execute ALL tool_use blocks CONCURRENTLY
                 |
                 v
     append assistant msg + ONE user msg containing
     ALL tool_result blocks  <-- critical: one message, all results
                 |
                 +----------> loop
```

Two rules people get wrong:

- **Return every `tool_result` in a single user message.** Splitting them across
  messages silently teaches the model to stop making parallel calls, and the
  agent gets slower every turn.
- **A failed tool still needs a `tool_result`** with `is_error: true`. Dropping it
  produces an API error — every `tool_use` block must be answered.

The loop is written by hand rather than using the SDK's `tool_runner` helper. The
helper is the right default in general, but three things here need per-turn
control it does not expose: the streamed progress display, per-tool timing, and a
hard turn ceiling. Swapping to `tool_runner` later is a contained refactor.

---

## 5. Two-phase design: agent for research, workflow for assembly

Letting an agent free-form the final itinerary produces beautiful prose that no
UI can parse. Instead:

**Phase 1 — Research (agent).** Open-ended tool-calling loop. Goal: gather facts.
Ends with a plain-text research brief.

**Phase 2 — Assembly (constrained calls).** Feed the research brief into calls whose
response is forced to match a Pydantic schema. Out comes a validated `Itinerary`
object: typed days, stages, distances, gear, costs.

The split matters because Phase 2 is _deterministic in shape_. It yields:

- guaranteed-parseable output for the CLI renderer, a web UI, or a database
- no "the model forgot the elevation field this time"
- lower cost — Phase 2 needs no tools and no exploration

This "agent to gather, workflow to commit" shape generalises to most real LLM apps.

### 5.1 Why Phase 2 is two calls and not one

Structured outputs work by compiling the JSON Schema into a constrained decoding
grammar. That grammar has a size limit, and the full `Itinerary` blows it:

```
400 invalid_request_error:
The compiled grammar is too large, which would cause performance issues.
Simplify your tool schemas or reduce the number of strict tools.
```

This only appears against the live API — every local test passed. Measured
variants, by serialised schema size:

| Variant                                            | Schema | Result    |
| -------------------------------------------------- | ------ | --------- |
| Full `Itinerary`, optionals + descriptions         | 9,593  | **fails** |
| All scalar optionals removed, descriptions dropped | 5,627  | **fails** |
| Narrative + days half                              | 4,889  | compiles  |
| Logistics + packing + budget half                  | 3,956  | compiles  |
| Narrative half with 400-char descriptions          | 3,755  | compiles  |

Two conclusions, and the second is the useful one:

1. Removing the `| None` optionals was **not** enough — raw structural weight is
   what matters, so degrading the schema's semantics buys little.
2. **Descriptions are essentially free.** A variant carrying 400-character
   descriptions still compiled. The grammar is driven by structure (nested models,
   field count, unions), not by prose.

So the fix is to split the _request_, not to strip the guidance that makes the
output good. `ItineraryCore` (narrative + days) and `ItineraryLogistics` (getting
there, bookings, packing, budget, safety) each compile comfortably, run
**concurrently** — they read the same brief and neither depends on the other — and
merge via `Itinerary.from_parts()`. `Itinerary` itself is defined by inheriting
from both halves, so there is still one source of truth for the field list, and it
is never sent to the API.

The general lesson: when a schema gets too big for structured outputs, partition it
along a seam where the halves are independent. The validation guarantee survives,
and the prompting usually improves for free because each call ends up with one job.

---

## 6. Model configuration

- **Model:** `claude-opus-5` — 1M context, strong long-horizon tool use.
- **Thinking:** `{"type": "adaptive", "display": "summarized"}`. Adaptive lets the
  model choose reasoning depth per turn. `display` is set explicitly because the
  default is `"omitted"`, which makes the terminal look frozen during long thinks.
- **Effort:** `output_config: {"effort": "high"}` for research (many tool calls,
  real trade-offs to reason about), `"medium"` for assembly.
- **Streaming:** always. `max_tokens` is large, and non-streaming requests with big
  `max_tokens` hit HTTP timeouts.
- **Prompt caching:** the tool list and system prompt are byte-stable and come first
  in the request, so they cache. Volatile content (today's date, the user's request)
  goes last. Watch `usage.cache_read_input_tokens` — if it is 0 across runs,
  something upstream is changing between calls.

---

## 7. Module layout

```
src/nature_trails/
├── core/
│   ├── config.py      Settings (env-driven), no secrets in code
│   ├── http.py        Cached + rate-limited + retrying HTTP client
│   └── geo.py         Bounding boxes, haversine, coordinate math
├── tools/
│   ├── registry.py    @tool decorator -> JSON Schema, the dispatch table
│   ├── places.py      geocode, elevation, daylight
│   ├── trails.py      Overpass: routes, huts, amenities, access
│   ├── weather.py     Open-Meteo forecast + climate normals
│   └── packing.py     Deterministic gear rules engine
├── agent/
│   ├── prompts.py     System prompts (research + assembly)
│   ├── schemas.py     Itinerary JSON Schema (the Phase-2 contract)
│   └── loop.py        The agentic loop + phase orchestration
├── render/
│   └── terminal.py    Itinerary -> rich terminal output / Markdown
└── cli.py             Entry point
```

**Dependency direction is strictly one-way:** `cli -> agent -> tools -> core`.
Nothing in `core` imports from `tools`; nothing in `tools` imports from `agent`.
That is what keeps the agent core UI-agnostic, so swapping the CLI for FastAPI
later touches exactly one file.

---

## 8. Why there is no mid-run questioning

A tool's implementation does not have to be code. An `ask_user` tool can be backed by
a person: the model emits a `tool_use` block, the CLI prints the question and waits
for input, and the answer returns as a `tool_result`. From the model's side that is
indistinguishable from querying Overpass.

This project built that, then removed it. The reasoning is worth more than the
feature was.

In a terminal the pause is free. The coroutine suspends on `input()`, the process
stays alive holding the whole conversation in memory, and it resumes when the user
types — no state management, because nothing was ever lost. On the web the same pause
requires the server to hold or persist the entire conversation across a network round
trip, and on serverless hosting, where execution is capped at seconds and no process
is long-lived, it is not possible at all. Since the roadmap targets a Next.js
frontend (see [WEB.md](WEB.md)), the feature would have been built twice and thrown
away once.

The replacement costs nothing and captures most of the value:

- **Collect preferences once, up front** (`agent/intake.py`). Five facts that are
  decision-relevant for every trip and almost never present in a free-text request:
  passport country, accommodation style, group size, exposure tolerance, date
  flexibility. Nationality alone changes two schema fields — an American needs
  Recreation.gov lotteries and an America the Beautiful pass, an EU citizen gets
  alpine-club reciprocity worth 30–50% off huts plus rescue cover.
- **Tell the model plainly that it has no channel back**, so ambiguity resolves into
  a stated assumption recorded in `unknowns` rather than a stall or a silent guess.

A questionnaire is a form on the web. A suspended coroutine is a distributed-systems
problem.

**Lesson:** when an elegant mechanism only works in one deployment target, price the
target that is actually wanted. Deferring a capability is cheaper than building it
twice.
