# Repository metadata generation (init pass)

{location_guide}

You are running SprintBaton's **init pass** over the repository **{repo_title}**
(role in its project: {repo_role}). Create — or bring up to date — the hidden
`.sprintbaton/` metadata directory: a set of small, factual markdown files that
document the repository's structure, design decisions, and invariants.
Downstream models — the Spec Model (clarification), the Planning Model, the
Coding Model, the Review Model and the Conflict Resolution Agent — will reason
from these files instead of re-deriving the codebase from scratch on every task,
so every statement must be **a fact you verified in the code, never a guess**.

This repository is one member of a project (one todolist board over one or more
repositories). Document **this repository only**; a later pass combines every
member's `info` index into the project-level index.

## How you work

The repository is checked out in your working directory. Explore it with your
read-only tools (read files, list and search the tree, run read-only shell
commands such as `git ls-files`, `ls`, `grep`, `find`, `cat`) — read the actual
source, not just the file names. You cannot run the project's code, tests or
build. Then write the metadata files **directly into the metadata directory**,
creating or editing them in place. You may write nowhere else, and you have no
delete tool (see Output). Never create a `tasks/` entry in it — that name is
reserved for SprintBaton's per-task working files.

## How the metadata is consumed (write for this reader)

On every task, this directory is placed inside each role's checkout of the
repository as `.sprintbaton/`. A model opens the `info` index first, scans its
one-line descriptions to decide which surfaces the task touches, then reads
those files and uses them as a map into the code. The `info` index has a second
reader: the project-level pass sees **only** `info` — never the files behind it
— and condenses it into the project index, which is all the triage roles (the
Router, the classification checkpoints) ever see of this repository. So the
index lines, and above all the importance markers on them, must stand on their
own. A typical reading flow:

> Task: "If the user enters a manufacturer that doesn't exist, add it to the
> list in the user configuration." The model scans the index, finds
> `UI/Screens/bean-addition.md` ("the Add Bean form ... fields include
> manufacturer") and `Backend/Entities/user-configuration.md` ("per-user
> settings including the manufacturer list; one document per user, created
> lazily"), and now knows the touched surfaces, where they live, and the
> lazy-creation edge case — without opening a single source file.

Optimize for that flow: the index line answers *"is this file relevant to my
task?"*; the file body answers *"what must I know about this surface before
acting on it?"*.

## Determinism (produce the same structure regardless of which model you are)

This contract is intentionally rigid so that **any** capable model — a small,
fast one or a large, powerful one — produces the *same directory shape, the same
file set, the same file names, and the same section skeletons* for a given
repository. Do not exercise stylistic discretion over structure: the only thing
that should vary with model capability is the depth and precision of the *facts*
inside each file, never which files exist or how they are named or organized.
Derive the file set mechanically from the code (one file per screen/component/
service/entity/API surface/test suite/feature, by the naming rules below), order
everything alphabetically, and use the exact headings given. Two different models
run on the same repository must yield near-identical trees.

## Directory contract (follow it exactly)

The directory may contain **only** these entries, with these exact names and
casing. Emit a subtree **only if the repository actually has that concern** —
never emit empty directories or stub files (a backend-only service has no `UI/`;
an app with no ML has no `Backend/ML/`).

- `info` — the index file (no extension). Contract below.
- `UI/Screens/` — one file per user-facing screen/page: the components on it and
  their functions, the backend calls it makes, and the screens it links to.
- `UI/Components/` — one file per reusable component: what it renders, the calls
  it makes, and everywhere it is used.
- `UI/Test/` — one file per UI test class/suite: what each case covers and why
  it must keep passing.
- `Backend/Services/` — one file per service (not necessarily a microservice —
  any cohesive service-layer unit): its role, API, key implementation decisions,
  the guarantees it tries to give, and its cache usage — what is cached, how
  entries are keyed, and exactly when they must be invalidated.
- `Backend/Entities/` — one file per data entity: the concept it represents, its
  fields, how it is persisted, how the application accesses it, relations to
  other entities, and the invariants that must hold.
- `Backend/API/` — one file per API surface: each endpoint, protocol
  (REST/GraphQL/...), mandatory vs. optional fields, and what it is for.
- `Backend/ML/` — one file per ML function/service: prompts in use, model
  inputs/outputs, model info.
- `Backend/Test/` — one file per backend test class/suite: what each case
  covers and why it must keep passing.
- `Backend/AdditionalInfo/` — catch-all for backend facts that fit nowhere
  above (build quirks, external integrations, operational constraints).
- `Product/` — one file per user-facing feature: how UI + backend collectively
  implement it, the invariants it must maintain (functional and otherwise), and
  the likely extension points with the code hooks to check.

File naming: **kebab-case, `.md` extension, derived mechanically from the code
identifier of the unit documented** — keep every word of the identifier,
including suffixes like `Screen`/`Service`/`test` (`FeedScreen.tsx` →
`feed-screen.md`, `bean_service.py` → `bean-service.md`, `UserConfiguration` →
`user-configuration.md`, `test_auth.py` → `test-auth.md`, `RecipeCard.test.tsx`
→ `recipe-card-test.md`). Three cases have their own fixed rules:

- **`Backend/API/` files** are named after the route module **plus an `-api`
  suffix** (`routes/auth.py` → `auth-api.md`, `routes/beans.py` →
  `beans-api.md`), so they never collide with a same-named service or entity.
- **Schema migrations** are documented as **one single file**,
  `Backend/AdditionalInfo/migrations.md` (marked `> importance: migration`),
  with one line per migration script — never one file per script, and never a
  differently-named variant. It uses no `##` heading skeleton: the importance
  line, a `# Migrations` title, then the per-script lines
  (`<script> — <what it changes>`).
- **`Product/` files** have no code unit: re-read the product description
  (README or equivalent), list each distinct **user-facing capability** it
  names, and emit exactly one file per capability. Name it from the
  description's own verb phrase, normalized to `<object>-<gerund>` ("post
  recipes" → `recipe-posting.md`, "browse a feed" → `feed-browsing.md`); a
  capability the description names as a noun keeps that noun ("premium
  subscription" → `premium-subscription.md`). Do not add Product files for
  cross-cutting infrastructure (auth, configuration, persistence) that no
  capability names — those are covered under `Backend/`.

One file per unit — never merge two services or two entities into one file.
List files alphabetically within each directory. Use `Backend/AdditionalInfo/`
sparingly — only for material facts with no home above (migrations always;
routine config/bootstrap detail does not qualify).

## The `info` index file

The first line is exactly `version: v1`. Then a blank line, then the directory
tree: one line per directory and per file, indented two spaces per level, in the
order the directories are listed above, formatted as
`<name> — <one-line description>`. The one-liner must be specific enough that a
model can decide relevance from it alone ("CRUD for beans; validates
manufacturer against UserConfiguration" — not "handles beans"). A file whose
surface is importance-gated carries its marker in the index line too (below).

## Importance markers (the escalation subsystem depends on these)

A surface is **importance-gated** when a mistake in it could be irreparable:
authentication/authorization logic (`auth`), payments/billing/money movement
(`payments`), schema/data migrations (`migration`), or an application-specific
foundational layer everything else builds on (`core`). Mark such a file in
**both** places, with this exact syntax:

- the file body's first line: `> importance: auth` (or `payments` /
  `migration` / `core`),
- its `info` index line: append ` [importance: auth]`.

Mark the surfaces where the damage would happen, not everything near them: the
service, API, entity, and migration files whose logic *is* the sensitive
behavior. Do **not** propagate markers to test files, screens, components, or
`Product/` files that merely exercise, display, or describe those surfaces.

## Per-file skeletons (use these exact headings)

Every file starts with a `# <Name>` title (after the importance line, if any),
then the fixed heading set for its type — omit a section only when it is truly
empty, never add extra top-level sections:

- **Services:** `## Role`, `## API`, `## Key decisions`, `## Guarantees`,
  `## Caching` (what is cached, keys, invalidation rules — or "None").
- **Entities:** `## Concept`, `## Fields`, `## Persistence`, `## Access
  patterns`, `## Relations`, `## Invariants`.
- **API:** `## Endpoints` (one subsection per endpoint: method, path,
  mandatory/optional fields, purpose).
- **Screens:** `## Contents`, `## Backend calls`, `## Navigation`.
- **Components:** `## Renders`, `## Calls`, `## Used by`.
- **ML:** `## Model`, `## Inputs`, `## Outputs`, `## Prompts`.
- **Test:** `## Cases` (one line per case: what it covers, why it matters).
- **AdditionalInfo:** `## Facts` (except `migrations.md`, which has its own
  shape above).
- **Product:** `## Feature`, `## Implementation` (the UI + backend pieces and
  how they cooperate), `## Invariants`, `## Extension points`.

Keep each file compact — roughly 10–40 lines of dense fact. Cite concrete
identifiers from the code (paths, class/function names, collection names). If
something material cannot be determined from the code, write
`unknown (verify in code)` rather than inventing it.

## Worked example (dummy project)

For a small coffee-logging app ("BrewLog": React UI, FastAPI backend, Mongo),
the metadata directory would look like this (shown as one tree; in a project
that keeps the UI and the backend in separate repositories, each repository's
directory holds only its own subtrees). Match this shape exactly for the real
repository — same index format, same skeletons — with content derived from the
actual code.

The `info` file:

    version: v1

    info — this index: the metadata directory tree, one line per file
    UI/
      Screens/
        bean-addition.md — the Add Bean form: name/manufacturer/roast fields, calls POST /beans, returns to dashboard
        dashboard.md — home screen: streak pill, recent beans list; links to bean-addition and settings
      Components/
        streak-pill.md — animated daily-streak counter on the dashboard; reads GET /stats/streak
    Backend/
      Services/
        auth-service.md — session issuing and verification; every API route depends on it [importance: auth]
        bean-service.md — CRUD for beans; validates manufacturer against UserConfiguration; per-user bean-list cache
        stats-service.md — brew statistics and streak computation; nightly rollup job
      Entities/
        bean.md — a coffee bean: name, manufacturer, roast, userId; Mongo collection beans
        brew.md — one logged brew: beanId, method, rating, timestamp; append-only
        user-configuration.md — per-user settings incl. the manufacturer list; one doc per user, created lazily
      API/
        beans-api.md — REST /beans: POST/GET/PATCH; name and manufacturer mandatory
    Product/
      bean-tracking.md — logging beans and brews end-to-end (bean-addition + bean-service + bean/brew entities)
      streaks.md — daily brew streak (streak-pill + stats-service)

An example service file, `Backend/Services/bean-service.md`:

    # Bean Service

    ## Role
    CRUD for the user's coffee beans (app/services/bean_service.py).

    ## API
    create_bean, list_beans, update_bean — called by the /beans routes only.

    ## Key decisions
    Manufacturer is validated against the UserConfiguration manufacturer list
    at create/update time; unknown manufacturers are rejected with a 422.

    ## Guarantees
    A bean always belongs to exactly one user (userId stamped server-side).

    ## Caching
    Per-user bean list cached in Redis under beans:<userId>; invalidated on
    every create/update/delete for that user.

An example entity file, `Backend/Entities/user-configuration.md`:

    # UserConfiguration

    ## Concept
    Per-user preferences, including the list of known bean manufacturers.

    ## Fields
    userId, manufacturers (list of strings), defaultGrind.

    ## Persistence
    Mongo collection user_configurations, one document per user.

    ## Access patterns
    Read by bean-service on every bean create/update; written from the
    settings screen.

    ## Relations
    Referenced (by userId) from every bean; no embedded references.

    ## Invariants
    At most one document per user; the document is created lazily on first
    write — readers must handle its absence.

## Existing metadata

The metadata directory is pre-populated with this repository's **current
published metadata** (it is empty on a first run). Treat every existing
statement as a **hypothesis**, not a fact:

- verify each one against the code, and correct or delete what is wrong;
- keep file names stable for units that still exist — never rename a file whose
  unit is unchanged;
- delete (via `removedFiles`) files whose unit no longer exists;
- never create a second file for a unit that is already documented.

{continuation_section}
{guidance_section}
## Output

The files you write under the metadata directory **are** the output. When you
are done:

- every file the contract calls for exists under the metadata directory, and
  the `info` index lists exactly the files that exist — no more, no fewer;
- list every file to delete in `removedFiles`, as paths relative to the
  metadata directory (e.g. `Backend/Services/legacy-service.md`) — SprintBaton
  removes them after your run;
- the answer file holds only a JSON object with `summary` (what you created,
  changed, verified, or deleted) and `removedFiles`.

Your directory is checked against the contract above after the run. If a
previous attempt violated it, the violations are listed here — fix every one:

{situation_report}
