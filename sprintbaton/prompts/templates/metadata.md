# Project & repository metadata

A **project** is one todolist board backed by one or more git repositories (for
example a `frontend` UI repo and a `backend` API repo). SprintBaton's
initialization run documents it at **two levels**:

1. **The project index** — the block shown at the end of this section. It says
   what the product is, names every member repository with its id and its role
   in the project, and states that each repository carries its own detailed
   metadata. **Start here** to understand how the repositories compose and to
   decide which one(s) your task touches. The index is generated once and
   deliberately states **no filesystem paths**.
2. **Each repository's own `.sprintbaton/` metadata** — inside that
   repository's checkout, with `.sprintbaton/info` as its index. It documents
   that codebase in detail (layout below).

**Where things are on disk** depends on your role and this turn. When you have
files to read, the block below opens with a generated *"Where things are on disk
(this turn)"* section listing the exact current location of each repository
checkout and of its `.sprintbaton/info`. Use those locations and no others —
never guess a path from a repository's name or id. When that section is absent
you have no checkout this turn: work from the project index and the task text
alone.

A checkout also holds this task's own working files under
`.sprintbaton/tasks/<task id>/`: `task.md` (the card as posted), `spec.md`,
`plan.md`, `passing-criteria.md`, `notes.md` (earlier roles' reasoning) and
`review-comments.md` (code-review findings, one section per round), with a
`metadata.md` index saying which of them exist yet. They are regenerated before
every turn — read them freely, but never edit them and never treat them as part
of the repository's code.

Each repository's own `.sprintbaton/` directory documents that codebase as small
factual files:

- `info` — the index: the directory tree with a one-line description per file
  (first line `version: ...`), so the right files can be identified at a glance.
- `UI/Screens/`, `UI/Components/`, `UI/Test/` — one file per screen (its
  contents, backend calls, navigation), per reusable component (what it
  renders, calls, and where it is used), and per UI test suite.
- `Backend/Services/` — one file per service: role, API, key decisions,
  guarantees, and cache usage with its invalidation rules.
- `Backend/Entities/` — one file per data entity: concept, fields, persistence,
  access patterns, relations, and invariants.
- `Backend/API/` — one file per API surface: endpoints, mandatory/optional
  fields, purpose. `Backend/ML/`, `Backend/Test/`, `Backend/AdditionalInfo/`
  cover ML functions, backend tests, and everything else.
- `Product/` — one file per feature: how UI + backend implement it, its
  invariants, and its extension points.

A repository has only the subtrees it needs (a backend-only service has no
`UI/`).

Files documenting **importance-gated surfaces** (auth / payments / migration /
core) are marked `[importance: ...]` in the index and `> importance: ...` at
the top of the file body — treat anything so marked as a surface where mistakes
may be irreparable.

## How to read it

Scan the project index to pick the repository (or repositories) your task
touches. If you have a checkout, open that repository's `.sprintbaton/info`,
scan its one-line descriptions to find the surfaces involved, then read the
files it points to. Treat what the metadata states as **documented fact about
the codebase — not guesses**, use it as a map into the code, and verify in the
code before acting on details the one-liners omit. A task may span several
repositories; scope the work across them the same way you would across the
modules of a single repository.

A worked reading flow — task: *"If the user enters a manufacturer that doesn't
exist, add it to the list in the user configuration."*

> The project index lists two repositories: *web-frontend* (the React UI) and
> *web-backend* (the REST API and data layer) — the task needs a screen and the
> data behind it, so both are candidates. Scanning each one's
> `.sprintbaton/info`: `UI/Screens/bean-addition.md` — "the Add Bean form:
> name/manufacturer/roast fields, calls POST /beans" — so the entry point is
> the bean-addition screen. `Backend/Entities/user-configuration.md` — "per-user
> settings incl. the manufacturer list; one doc per user, created lazily" — so
> the list lives on UserConfiguration, and a user may not have the document yet
> (an edge case to handle). `Backend/Services/bean-service.md` — "validates
> manufacturer against UserConfiguration; per-user bean-list cache" — so the
> validation site is bean-service and a cache may need invalidating. Three index
> lines pinned down the affected surfaces, the edit sites, and two edge cases —
> and showed the form already sends the manufacturer, so the change is in
> *web-backend* alone.

If the block below says no project metadata has been generated yet, reason from
the task text (and the code, when you have a checkout) alone, and lean toward
more careful handling.

Project metadata for this task — the location guide for this turn (when you
have a checkout), then the project index:

{metadata_summary}
