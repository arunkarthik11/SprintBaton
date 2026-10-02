"""Clarification Translator — renders model questions and situation reports
into provider-facing todolist comments."""

from sprintbaton.entities.clarification import ClarificationOptions

QUESTION_HEADER = "🤖 **SprintBaton needs your input**"
CLARIFICATION_HEADER = "🤔 **SprintBaton needs one decision to continue**"
REPORT_HEADER = "🛑 **SprintBaton is blocked and needs a human decision**"
PLAN_HEADER = "📋 **SprintBaton plan** (task moves to In Progress once accepted)"
PR_HEADER = "✅ **SprintBaton opened a pull request**"
QA_HEADER = "🚦 **SprintBaton cut a release** (this task moved to QA)"
# Card edits and board moves SprintBaton reacted to (task-revisions spec §8)
REVISION_HEADER = "🔁 **SprintBaton noticed a change to this card**"

# Every header the agent ever posts a comment under. Used to recognize the
# agent's own comments on providers that don't expose a comment author id
# (conversation-lifecycle spec §13 — the Todoist REST comment shape).
AGENT_COMMENT_HEADERS = (QUESTION_HEADER, CLARIFICATION_HEADER, REPORT_HEADER,
                         PLAN_HEADER, PR_HEADER, QA_HEADER, REVISION_HEADER)


class ClarificationTranslator:
    def render_clarification_question(self, action: str, question: str,
                                      options: ClarificationOptions | None = None
                                      ) -> str:
        """A single blocking question from any task-action role
        (conversation-lifecycle spec §6): the task pauses in place, reassigned
        to the human, and continues as the same agent once answered. With
        structured options (clarification-options spec §5) the question gains
        a numbered choice list — one option per line — and the reply
        instruction invites a number; without them, output is byte-for-byte
        what it was before options existed."""
        lines = [
            f"{CLARIFICATION_HEADER} (from the {action.replace('_', ' ')} step)",
            "",
            question,
        ]
        if options is not None and options.answers:
            lines.append("")
            for i, answer in enumerate(options.answers, start=1):
                label = answer.option + (" (Recommended)" if answer.isRecommended else "")
                lines.append(f"{i}. {label}" +
                             (f" — {answer.description}" if answer.description else ""))
                if answer.additionalNotes:
                    lines.append(f"   {answer.additionalNotes}")
            instruction = (
                "Reply with one or more numbers (comma-separated) or your own answer, "
                if options.multiSelect else
                "Reply with a number or your own answer, ")
            closing = (instruction +
                       "then reassign the task to the agent; "
                       "work resumes exactly where it paused.")
        else:
            closing = ("Answer in a comment and reassign the task to the agent; "
                       "work resumes exactly where it paused.")
        lines += ["", closing]
        return "\n".join(lines)

    def render_questions(self, questions: list[str]) -> str:
        lines = [QUESTION_HEADER, "",
                 "Please answer in a comment on this task; work resumes on your reply.", ""]
        lines += [f"{i}. {q}" for i, q in enumerate(questions, start=1)]
        return "\n".join(lines)

    def render_situation_report(self, report_summary: str, report_url: str | None,
                                approval_required: bool = False) -> str:
        lines = [REPORT_HEADER, "", report_summary]
        if report_url:
            lines += ["", f"Full situation report: {report_url}"]
        if approval_required:
            lines += ["", "Reply **approve** to allow the blocked operation, or describe what to do instead."]
        return "\n".join(lines)

    def render_plan_notice(self, plan_url: str) -> str:
        return f"{PLAN_HEADER}\n\nPlan: {plan_url}"

    def render_pr_notice(self, pr_url: str, summary: str) -> str:
        return f"{PR_HEADER}\n\n{pr_url}\n\n{summary}"

    def render_qa_notice(self, release_title: str, promotion_pr_url: str | None) -> str:
        lines = [QA_HEADER, "",
                 f"Swept into **{release_title}** with the rest of this window's batch.",
                 "Regression-QA the batch on staging; reassign any task in the batch to "
                 "the agent to sign the whole release off."]
        if promotion_pr_url:
            lines += ["", f"Cutover PR (dev → staging): {promotion_pr_url}"]
        return "\n".join(lines)

    def render_revision_notice(self, text: str) -> str:
        """A card edit or board move SprintBaton reacted to (task-revisions
        spec §8.4/§8.6/§8.7): what it noticed and what it is doing about it."""
        return f"{REVISION_HEADER}\n\n{text}"
