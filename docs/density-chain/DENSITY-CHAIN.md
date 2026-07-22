# The MASH Density-Trellis — a branching chain-of-density map of the whole engine

**Status: orientation artifact, reverse-engineered July 21, 2026** by OpenCnid
(from a public clone of the repo), using the
[`density-chain`](https://github.com/OpenCnid/chain-of-density) method in system
mode. PROPOSED / unofficial — a *reader's map*, not an authority. If any sentence
disagrees with the code, the code wins. Counts and file:line locators are as of
the clone read this session and drift with the repo.

MASH is **Matthew Murphy's (Mogura / Lexideck)** engine
([github.com/gusthemole/MASH](https://github.com/gusthemole/MASH), Artistic
License 2.0). This map lives in OpenCnid's fork with the original `LICENSE` and
attribution intact; it was produced because MASH turned out to be a strikingly
close *sibling design* to OpenCnid's Trellis engine — see the correspondence note
at the end.

> **Rendered companion.** An interactive, theme-aware HTML render of this trellis
> lives beside this file at [`DENSITY-CHAIN.html`](DENSITY-CHAIN.html). The
> markdown is ground truth; the HTML is the map.

## How to read this file

One shared **trunk** (the whole engine at three densities) plus one **branch per
subsystem class**, each branch a fixed-length five-tier chain whose tiers densify
**general essence (T1) → current shipped machinery (T2–T3) → the frontier / limits
(T4–T5)**. Each tier is complete on its own; a deeper tier *adds*, never corrects.
Bold marks an entity's first introduction in its branch.

---

## The trunk — the whole engine at three densities

### Trunk-T0 (one sentence)

MASH is a single-process, shared-memory **"semantic reality engine"** — a
TinyMUSH-descended text world of rooms, objects, and agents held in one
JSON-backed database, driven by a hardcoded-plus-softcoded command engine that
routes every sensory or narrative gap through a Gemini **"Loom"** whose bracketed
output is re-parsed as commands in the *same* syntax players and softcode use.

### Trunk-T1 (one paragraph)

The world is one **`dict[dbref → GameObject]`** — rooms, exits, items, and agents
are all one flat dataclass distinguished by a `type` string — persisted whole-file
to a gitignored **`world.json`**. A **"dumb orchestrator"** (`MashEngine`)
dispatches a raw command first against **hardcoded kernel verbs**, then falls
through to **softcoded `$`/`^` triggers** stored as attributes on objects. Every
command is registered *with* its own `{help, category, aliases, usage}` metadata,
so a single `help` renders the whole catalog. When a command needs a description
or an NPC needs to act, the engine composes a prompt from `object.to_dict()`
context and calls a Gemini **Loom** (Sensory for narration, Visual for images);
the model's bracketed output (`[go north]`, `[memo …]`) is re-parsed by the same
dispatcher — **narration, action, and softcode share one wire format**. Tokens
price world-building (`@dig`/`@create`/`@agent`); the whole thing runs in one
Streamlit process shared across all connected users.

### Trunk-T2 (the class map — the nine branches)

- **C1** World & object model — `GameObject`, `WorldDatabase`, `world.json`.
- **C2** Command engine & the shared wire — `MashEngine.process_command`, dispatch order.
- **C3** Softcode & function evaluation — `$`/`^` triggers, `[func()]`, `%n` placeholders.
- **C4** Self-documenting commands & help — `command_meta`, `_cmd_help`, categories.
- **C5** The AI Loom surface — Gemini, Sensory/Visual Looms, the "bloom."
- **C6** Autonomous agents & the robot loop — `get_ai_context` → `capture_robot_intent`.
- **C7** Economy, permissions & world-building — tokens, ownership/locks, `@dig`/`@create`/`@agent`.
- **C8** Subjective VR (the Holodeck) — per-player room overlays, the Architect & Dungeon-Master Looms.
- **C9** The Streamlit shell — the shared-singleton world, per-user session, the "alive" sidebar.

The weave: **C2** dispatches into **C1** and, on a miss, **C3**; **C4** documents
**C2**'s verbs; **C5** is called by **C2** whenever narration is needed and by
**C6**/**C8**; **C6** and **C8** are **C5** pointed back into the world through
**C2**'s wire; **C7** gates who may mutate **C1**; **C9** wraps the whole engine
as one shared process.

---

## The branches

### C1 — World & object model
*One flat object graph, JSON-persisted; the closest MASH thing to "the facts."*

- **T1 — essence.** Everything in the world — a room, an exit, an item, an
  autonomous agent — is one **`GameObject`** dataclass distinguished only by a
  `type` string, addressed by a **`dbref`**, and holding ~35 optional fields plus
  a free-form **`attrs`** string-map. The whole world is a single
  `dict[dbref → GameObject]` in memory.
- **T2 — current machinery.** `WorldDatabase` (`database.py:144`) holds
  `self.objects` plus name/type/location indices "for O(1) performance"; `load()`
  (`:184`) JSON-parses `world.json` and reconstructs each object, **silently
  dropping unrecognized keys** (`:202`); `save()` (`:219`) dumps the entire dict.
- **T3 — with receipts.** Persistence is whole-file, atomic via temp-file +
  `os.replace` (`database.py:229-241`) — "Atomic swap!" — with no diffing, no
  history, no per-user partition. `world.json` is declared *runtime/secret state*
  in `.gitignore` (grouped with `.env`, `*.key`), never versioned.
- **T4 — the frontier.** `to_dict()` (`database.py:91`) strips empty fields and is
  the universal object→context primitive every AI call and command is built from.
  The one durability cadence is a 30-minute autosave plus wizard `@dump`/`@reload`
  (`app.py:369`, `:483`).
- **T5 — the limit.** `attrs: Dict[str,str]` is an unschema'd bag written directly
  by handlers (VR descriptions, outfit slots, `body_desc`); the silent field-drop
  on load *is* the schema-drift failure a typed, custody-bearing store avoids. The
  world is mutable game-state — last-write-wins, no provenance.

### C2 — Command engine & the shared wire
*The "dumb orchestrator": parse, dispatch, mutate, narrate — one command language for players, softcode, and the AI alike.*

- **T1 — essence.** `MashEngine` is documented as "the 'dumb orchestrator' — it
  parses commands, updates state, and calls out to the AI layer for descriptions
  and NPC actions" (`mash_engine.py:6`). It holds a `commands: Dict[str,Callable]`
  and dispatches raw input against it.
- **T2 — current machinery.** `process_command` (`mash_engine.py:552`) resolves
  shortcut prefixes (`"`/`:`/`&`), bare exit names, then a hardcoded-verb lookup;
  **only on a miss** does it fall through to scan the room for softcode `$`
  triggers (`:583-595`). Hardcoded commands strictly pre-empt softcode.
- **T3 — with receipts.** ~115 methods live on one 4,527-line class
  (`mash_engine.py`, two classes total: `CommandResult` `:43`, `MashEngine`
  `:52`); "subsystems" are comment-banner sections, not modules. Commands mutate
  `GameObject` state and propagate via history / room broadcasts.
- **T4 — the frontier.** The engine's **most distinctive move**: the AI's output
  and the player/softcode input share **one bracket-command syntax** (`[cmd
  args]`), parsed by the same machinery (`capture_robot_intent`, `:2576`). There
  is no separate "tool-calling" format — narration *is* action.
- **T5 — the limit.** The monolith has no internal decomposition or test suite;
  correctness rests on one dispatcher and a flat verb table. The elegance (one
  wire) is also the risk: model output flows into the command parser with only
  regex extraction between them.

### C3 — Softcode & function evaluation
*User-scripted, live extensibility — the extensible half of the kernel/softcode split.*

- **T1 — essence.** Any object flagged `listening=True` can carry attributes
  formatted **`$pattern:action`** (command-shaped triggers) or **`^pattern:action`**
  (ambient, fire on room speech); matched input re-enters `process_command` *as the
  object*, not the player.
- **T2 — current machinery.** `$` shadow commands glob-match via `fnmatch`
  (`mash_engine.py:780-817`); `^` listen patterns fire on broadcasts
  (`:819-842`). Both run the stored action string through
  `_evaluate_functions` and `_substitute_placeholders` first.
- **T3 — with receipts.** Inline **`[func()]`** calls (`_evaluate_functions`,
  `:844`) cover `[rand()]`, `[pick()]`, `[v()]`, `[get()]`, math; **`%n`**
  placeholders (`_substitute_placeholders`, `:982`) expand `%0`/`%!`/`%l`/`%#`.
  `{ }` blocks sequence commands by newline or `;` (`README.md:132-176`).
- **T4 — the frontier.** This is TinyMUSH softcode reborn: a full mini-interpreter
  for player-authored behavior, live the instant an `&ATTR` is set. `@agent`
  drops an *autonomous NPC* (`:146`) that couples softcode to the AI loop (C6).
- **T5 — the limit.** A softcode string is gated only by object ownership
  (`can_modify`, `mash_engine.py:2361`) — no manifest, no review, no provenance,
  no lifecycle. It runs forever until an owner edits or deletes it; nothing
  "contests" it when its assumptions change.

### C4 — Self-documenting commands & help
*Building a command includes its documentation — MASH's discoverability-by-default, and the seed of Trellis's `llm_help`.*

- **T1 — essence.** MASH commands are **self-documenting by construction**: each
  registers *with* its metadata, so the engine can always answer "what commands
  exist and how do I use them?" without a separate doc pass.
- **T2 — current machinery.** `command_meta: name → {help, category, aliases,
  usage}` and `function_meta` (`mash_engine.py:67-68`) are populated by
  `register_command`/`register_function` (`:306-337`) — **one call site binds the
  handler and its docs together**.
- **T3 — with receipts.** ~60 commands are declared inline with their fields
  (`:92-282`, e.g. `category='Movement', usage='go <exit>', help='Move through an
  exit'`), grouped into Movement / Senses / Communication / Economy / Building /
  Ownership / System / VR. `_cmd_help` (`:1835-1927`) auto-generates the
  categorized catalog and per-item / per-topic lookup from that registry.
- **T4 — the frontier.** The registry serves five lookup shapes from one data
  structure (`help`, `help <cmd>`, `help <function>`, `help <topic>`,
  `help <category>`) — a clean precedent for a single "alive catalog" surface.
- **T5 — the limit.** The self-documentation is **human-help only** — it never
  reaches the UI (the sidebar is hand-coded, `app.py:465`) or the model
  (`ai_layer.py` has no reference to `command_meta`). And the help text can
  **drift from enforcement**: `@mind`'s help reads "(Wizard only)" (`:175`) while
  the real gate is a separate `if not agent.wizard` check (`:3272`) — nothing
  binds them.

### C5 — The AI Loom surface
*Where object state becomes a prompt — the Gemini layer, code-composed then model-bloomed.*

- **T1 — essence.** An **`AIEngine`** (`ai_layer.py`) wraps Google's GenAI SDK
  (Gemini text + image) behind named **"Looms"** — persona-fixed prompt builders
  for narration, NPC reasoning, image prompting, VR, and search-grounded research.
- **T2 — current machinery.** A **Sensory Loom** (`ai_layer.py:32`) is a single
  static `system_prompt` prepended to every text call; each Loom method
  code-assembles a prompt list from a `context` dict (`.get()` + f-strings,
  `:82-98`) — **no model in the composing step**.
- **T3 — with receipts.** `get_ai_context` (`mash_engine.py:2500`) is the one
  place engine objects (`agent`, `target`, room, exits, outfits) become that dict,
  entirely via `to_dict()`. The **Visual Loom** (`ai_layer.py:334`) "converts MASH
  state into a high-fidelity image prompt."
- **T4 — the frontier.** The **"bloom"** (`ai_layer.py:365`): the code-built image
  prompt is first sent to the *text* model to embellish it before the *image*
  model renders — one model composing another model's input. It is MASH's clearest
  "each function composes the next's prompt" move.
- **T5 — the limit.** The bloom is **ungated** — free model text flows to the next
  model with no derivation or grounding check. And the seven Loom methods each
  duplicate their own ad-hoc prompt assembly — the same instance repeated with no
  shared composition primitive.

### C6 — Autonomous agents & the robot loop
*NPCs that think in the same command language they act in.*

- **T1 — essence.** An `@agent` NPC runs a loop: the engine packages its situation
  as context, the AI reasons, and the AI's **bracketed output is re-executed as
  commands** — the agent writes in the exact language the engine parses from
  players and softcode.
- **T2 — current machinery.** `process_command`'s tick branch (`mash_engine.py:743`)
  calls `AIEngine.get_robot_tick` (`ai_layer.py:257`); **`capture_robot_intent`**
  (`mash_engine.py:2576`) regex-extracts `[go north]`, `[memo …]`, `[status …]`,
  `[goal …]` and re-dispatches each through `process_command`.
- **T3 — with receipts.** `[remember <text>]`/`[memo …]` append straight into
  `GameObject.memo`/`.status` — flat strings with hard 5000/2000-char caps that
  force lossy "consolidation" on overflow ("SYSTEM ALERT: Memory Full … perform a
  [memo <summary>] now", `:2611`).
- **T4 — the frontier.** This is agentic behavior with **zero tool schema**: the
  model's "tools" are the game's verbs, discovered from the world and the help
  text, exercised through the shared wire. It is a working end-to-end agent loop
  in a few hundred lines.
- **T5 — the limit.** The NPC memory is unbounded LLM prose written back with no
  provenance, deduplication, or trust standing — last-write-wins, lossy on
  overflow. It is the un-guarded form of a working-memory workspace.

### C7 — Economy, permissions & world-building
*Who may change the world, and what it costs to build it.*

- **T1 — essence.** MASH is user-extensible at the *content* layer: players spend
  **tokens** to create rooms, objects, and agents, and permission is a simple
  **ownership / wizard** check with a small **lock grammar**.
- **T2 — current machinery.** `@dig`/`@create`/`@agent` (`mash_engine.py:2932-3041`)
  spend tokens and call `db.create_object` synchronously, **live-instantiating**
  into the world the same turn. `is_owner`/`can_modify` (`:343-354`) gate mutation.
- **T3 — with receipts.** Costs are risk-scaled constants: `COST_CREATE = 10`,
  `COST_DIG = 50`, `COST_AGENT = 500` ("premium!", `:28`). Locks parse a grammar
  (`#dbref`, `!flag`, `object:`, `vehicle:`). A `TOKEN_DROP_TIERS` lottery seeds
  the economy.
- **T4 — the frontier.** Cost-scaled-to-blast-radius is a clean idea: a whole
  autonomous agent costs 50× an object. Building is instant and needs no review —
  the world grows live.
- **T5 — the limit.** Doc and code drift: `README.md:96` advertises `@agent` at
  **5 tokens**, the code enforces **500** (`:28`, `:3016`) — the same
  help-vs-enforcement gap C4 shows, in the economy. No staging, no review, no
  provenance on created content.

### C8 — Subjective VR (the Holodeck)
*Per-player realities layered over the shared world.*

- **T1 — essence.** In a `vr_ok` room, the engine can hand off to AI "Architect"
  and "Dungeon-Master" Looms that generate **per-player overlays** — one shared
  room, different subjective descriptions and exits per agent.
- **T2 — current machinery.** An unrecognized command (or a failed `go`, a `pose`,
  a `say`) in a VR room (`mash_engine.py:598-733`) calls `AIEngine.evolve_room`
  ("The Architect", `ai_layer.py:433`) or `react_to_vr` ("The Dungeon Master",
  `:495`).
- **T3 — with receipts.** Subjective state is written to
  `loc.attrs[f"_vr_desc_{agent.dbref}"]` — shadow overlay attributes on the shared
  room; look/exit interception (`:1035-1070`) renders each player their own view.
- **T4 — the frontier.** Architect→DM can chain via a `[scene_change]` bracket —
  again the shared wire, now driving generative world evolution. It is the most
  ambitious use of the Loom: the world itself is model-authored per player.
- **T5 — the limit.** Overlays live in the same unschema'd `attrs` bag (C1's
  limit), keyed by dbref; there is no lifecycle or cleanup discipline recorded in
  the clone.

### C9 — The Streamlit shell
*One shared-memory MUSH server wearing a web app.*

- **T1 — essence.** MASH runs as a **single Streamlit process** holding **one
  shared** `WorldDatabase`/`MashEngine` singleton across *all* connected browser
  sessions — a classic single-process MUD server, not a per-session web app.
- **T2 — current machinery.** `get_shared_database()` / the engine are
  `@st.cache_resource` singletons (`app.py:183`, `:218`); per-user Streamlit
  session state layers auth and view on top; a 3-second `st.fragment` poll
  (`app.py:54`) syncs across sessions via each agent's `message_buffer`.
- **T3 — with receipts.** `WORLD_FILE` is resolved once (`app.py:91`); wizard
  wizards (`is_wizard`, `handle_wizard_command`, `:465`) and the construction /
  outfit menus are hand-coded Streamlit UI.
- **T4 — the frontier.** The **"alive" sidebar** — the README's *"monitors your
  state and environment to surface relevant tools"* — is a state-aware
  discoverability surface (though hand-coded, not generated from `command_meta`).
- **T5 — the limit.** One shared mutable singleton across all users is simple but
  unpartitioned; concurrency safety rests on Streamlit's execution model and the
  whole-file save. `requirements.txt` (UTF-16) lists pandas/numpy/networkx that no
  sampled hot path uses.

---

## The temporal cross-section — essence → current → frontier/limit, all nine at once

| Class | What it *is* | Current mechanism | Frontier / distinctive | The limit (the un-guarded edge) |
|---|---|---|---|---|
| **C1 World/object** | one flat `GameObject` graph in `world.json` | O(1)-indexed dict, whole-file atomic save | `to_dict()` universal context primitive | `attrs` catch-all, silent field-drop, no provenance |
| **C2 Command engine** | the "dumb orchestrator" | hardcoded-first, softcode-fallback dispatch | one bracket wire for player/softcode/AI | 4,527-line monolith, no tests, model→parser |
| **C3 Softcode** | live user-scripted triggers | `$`/`^` patterns + `[func()]` + `%n` | TinyMUSH softcode reborn; `@agent` NPCs | ownership-only gate, no lifecycle/provenance |
| **C4 Self-doc & help** | commands carry their own docs | `command_meta` + `_cmd_help` catalog | one registry, five lookup shapes | human-help only; help can drift from enforcement |
| **C5 AI Loom** | object state → prompt | static Sensory Loom + `to_dict` context | the "bloom" (model composes next model's prompt) | bloom ungated; seven Looms, no shared primitive |
| **C6 Robot loop** | NPCs act in the command language | `get_ai_context` → `capture_robot_intent` | agentic loop with zero tool schema | unbounded LLM memory, lossy, no provenance |
| **C7 Economy/perms** | tokens + ownership build the world | `@dig`/`@create`/`@agent`, cost-scaled | cost-scaled-to-blast-radius; live building | doc/code cost drift; no review/staging |
| **C8 Subjective VR** | per-player realities | Architect/DM Looms → `_vr_desc_*` overlays | model-authored world per player | overlays in the same unschema'd `attrs` bag |
| **C9 Streamlit shell** | one shared MUD server as a web app | `@st.cache_resource` singleton + poll sync | the "alive" state-aware sidebar | unpartitioned shared singleton |

---

## Provenance & the Trellis connection

**How this was made.** Reverse-engineered July 21, 2026 from a read-only public
clone, via six parallel analysis agents reading `mash_engine.py`, `ai_layer.py`,
`database.py`, `app.py`, and `README.md`, each returning locator-grounded findings.
The clone is shallow and carries no design records or roadmap (a referenced
`mash_mcp_architecture.md` and a `reference_tinymush/` tree are gitignored and
absent) — so the T5 "future" tiers are read as *limits and frontiers visible in
the code*, not a stated roadmap. Every file:line is from the clone this session.

**Why OpenCnid mapped it.** MASH is a strikingly close *sibling* of OpenCnid's
**Trellis** engine — enough that Trellis adopted a design direction from it. Both
draw the same **kernel / extensible-userspace** line, both compose prompts from
**state objects**, both make commands/modules **self-documenting**, and both hold
long-lived state as an in-memory object graph flushed to JSON. MASH's author frames this as a **REPL over `world.json`**: the
file is the *fact-workspace*, and the doubt, belief, and fact workspaces are
variable values the loop reads and mutates. Trellis trust-grades those same REPL
variables and adds custody to them; MASH keeps one untyped workspace where
last-write-wins stands. The difference is **stakes and guards**: MASH is a lower-stakes narrative engine where a model may
freely author state and prompts (the ungated "bloom," the `attrs` catch-all, the
help-vs-enforcement drift), while Trellis is an epistemic engine that constrains
exactly those moves (typed provenance, guard-derived explanations, pinned
attribution). Read through Trellis, **MASH is the un-guarded twin** — and it
solved two problems Trellis had named but not yet surfaced: *agent-facing
discoverability* (its help system → Trellis's proposed `llm_help`) and *composed
intent from state objects* (its Loom → Trellis's per-object descriptor
discipline). The full analysis lives in the Trellis repo at
`docs/architecture/SELF_DESCRIBING_SURFACES.md`.

**Credit.** MASH is Matthew Murphy's (Mogura / Lexideck). This map is
appreciation, not appropriation — the engine, and the two ideas OpenCnid is
building on, are his.
