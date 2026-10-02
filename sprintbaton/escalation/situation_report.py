"""Situation reports (escalation spec §7).

Cross-tier state transfer is a rebuilt summary, never a transcript dump: a
higher tier must reason from clean state rather than inherit the lower tier's
wrong mental model. Conversation-resume is same-role only.
"""


def build_situation_report(
    *,
    task_title: str,
    task_description: str,
    plan: str | None,
    diff: str,
    failure_evidence: str,
    hypothesis: str | None,
    attempts_ruled_out: list[str],
) -> str:
    sections = [
        "# Situation report",
        "",
        "## Original task",
        f"**{task_title}**",
        "",
        task_description or "(no description)",
        "",
        "## Current plan / spec",
        plan or "(none)",
        "",
        "## Current diff",
        "```diff",
        diff[:30_000] or "(no changes yet)",
        "```",
        "",
        "## Failure evidence",
        failure_evidence or "(none captured)",
        "",
        "## Executor's hypothesis",
        hypothesis or "(none)",
        "",
        "## Already tried and ruled out",
    ]
    if attempts_ruled_out:
        sections += [f"- {a}" for a in attempts_ruled_out]
    else:
        sections.append("- (nothing yet)")
    sections += [
        "",
        "_The failed execution transcript is deliberately excluded; reason from this clean state._",
    ]
    return "\n".join(sections)
