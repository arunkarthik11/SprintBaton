from enum import StrEnum


class UserType(StrEnum):
    HUMAN = "HUMAN"
    AGENT = "AGENT"


class CredentialProvider(StrEnum):
    """Third-party services a user can supply their own API token for
    (user-multitenancy spec §7). Deliberately excludes infrastructure
    credentials (Mongo/Redis/MinIO) — no end user has their own MinIO bucket;
    those stay deployment-level Settings values unconditionally."""

    ANTHROPIC = "anthropic"
    # A Claude subscription token (`claude setup-token`), stored exactly like
    # an API key (hosted-sandbox-isolation spec §9.1). Distinct from ANTHROPIC
    # so a user may hold a default of each; a subscription-capable harness
    # prefers it when present (§9.2).
    ANTHROPIC_SUBSCRIPTION = "anthropic_subscription"
    GITHUB = "github"
    TODOIST = "todoist"
    LINEAR = "linear"
    OPEN_HANDS_LLM = "open_hands_llm"
    OPENAI = "openai"      # provider-registration spec §4.2
    GEMINI = "gemini"      # provider-registration spec §4.2


class TaskStatus(StrEnum):
    TaskPending = "TaskPending"
    TaskFinalization = "TaskFinalization"
    PassingCriteria = "PassingCriteria"  # acceptance-criteria checkpoint (machine-only)
    TaskFinalized = "TaskFinalized"      # spec classification checkpoint (machine-only)
    PlanFinalization = "PlanFinalization"
    PlanFinalized = "PlanFinalized"      # plan classification checkpoint (machine-only)
    InProgress = "InProgress"
    CodeReview = "CodeReview"            # pre-PR Review Agent gate (machine-only)
    InReview = "InReview"
    QA = "QA"
    Shipped = "Shipped"
    Blocked = "Blocked"  # EH halts (Blocked / Needs Input)
    # Column-less, non-terminal status of an invisible ProjectInitialization
    # task (project-initialization-task spec §4.2). Held through retries,
    # continuations, and usage-limit pauses; ends in Shipped or Blocked. A
    # Development task never reaches it.
    ProjectInitialization = "ProjectInitialization"


class TaskKind(StrEnum):
    """What a Task row is (project-initialization-task spec §4.1). Every board-
    ingested card is Development; ProjectInitialization is the invisible,
    queued metadata init pass — no card, no column, no adapter calls."""

    Development = "Development"
    ProjectInitialization = "ProjectInitialization"


class MetadataScope(StrEnum):
    """Which member repos an init run regenerates (spec §4.1)."""

    Missing = "missing"   # only repos with no published revision
    All = "all"           # every member repo


class InitializationTrigger(StrEnum):
    """Who created an init run — observability only; retry behavior never
    depends on it (spec §5.6)."""

    ProjectApply = "project_apply"   # automatic: project create/apply (§5.1)
    CliInit = "cli_init"             # explicit: `sprintbaton init`
    Api = "api"                      # explicit: POST /projects/{id}/metadata


class RepoWorkStatus(StrEnum):
    """Per-repo pipeline position for a multi-repo task (multi-repo-project
    spec §4.3). The task's flat TaskStatus is *derived* from the slowest
    (least-advanced) repo across this enum's order."""

    Pending = "Pending"        # identified as affected, not yet executed
    InProgress = "InProgress"  # executing / re-executing after a bounce
    CodeReview = "CodeReview"  # pre-PR Review Agent gate for this repo
    PrOpen = "PrOpen"          # PR opened, in human review
    NoOp = "NoOp"              # coding model produced no change for this repo
    Merged = "Merged"          # PR merged into this repo's devBranch
    Blocked = "Blocked"        # parked for this repo (escalation/conflict exhausted)


class ReleaseStatus(StrEnum):
    """Lifecycle of a release window batch (three-branch promotion spec §5)."""

    InQA = "InQA"        # cut from dev to staging, under regression QA
    Shipped = "Shipped"  # signed off, promoted staging -> production


class TaskType(StrEnum):
    """The Router's category — orthogonal to importance, which lives on
    Task.important (classification-taxonomy spec §3)."""

    Simple = "Simple"
    Ambiguous = "Ambiguous"
    Complex = "Complex"
    Abstract = "Abstract"


class SpecClassificationVerdict(StrEnum):
    """Spec Classification checkpoint outcome (classification-taxonomy spec §4)."""

    Simple = "Simple"      # directly implementable, Sonnet execution
    Compound = "Compound"  # directly implementable, Opus execution
    Complex = "Complex"    # needs a plan first


class PlanClassificationVerdict(StrEnum):
    """Plan Classification checkpoint outcome (classification-taxonomy spec §5)."""

    Simple = "Simple"      # Sonnet execution
    Compound = "Compound"  # Opus execution


class RewindPoint(StrEnum):
    """The earliest pipeline stage a card edit invalidates (task-revisions
    spec §7.4). Rewinding to a stage reruns it and everything downstream.
    Declared in pipeline order — `REWIND_ORDER` relies on it."""

    NoChange = "NoChange"
    Execution = "Execution"
    Planning = "Planning"
    PassingCriteria = "PassingCriteria"
    Finalization = "Finalization"
    Classification = "Classification"


class EscalationTier(StrEnum):
    """The escalation ladder (escalation spec §3; fable-coding-tier spec §3 renames the
    terminal human tier from a position-based name to a role-based one, and §4 inserts
    E4 as a coding tier in the position that frees up)."""

    E0 = "E0"  # In-tier retry (Sonnet, clean context)
    E1 = "E1"  # Lateral -> clarification (Spec loop)
    E2 = "E2"  # Planning escalation (Opus plans, Sonnet executes)
    E3 = "E3"  # Opus execution
    E4 = "E4"  # Fable execution — the highest coding-capability rung, reached only by
               # escalation from E3, never as an entry tier (fable-coding-tier spec §4)
    EH = "EH"  # Human handoff (async) — named by role, not position, so future coding
               # tiers (E5, E6, ...) never require renaming this value again


class TriggerType(StrEnum):
    """Machine-observable escalation triggers (escalation spec §5-§6)."""

    Thrashing = "Thrashing"
    CorrectnessStall = "CorrectnessStall"
    DiscoveredAmbiguity = "DiscoveredAmbiguity"
    PlanBreakage = "PlanBreakage"
    BudgetExhaustion = "BudgetExhaustion"
    ReviewFailure = "ReviewFailure"
    # Hard triggers — bypass the ladder
    DiscoveredImportance = "DiscoveredImportance"
    IrreversibleOperation = "IrreversibleOperation"
    GlobalCircuitBreaker = "GlobalCircuitBreaker"
