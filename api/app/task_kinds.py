"""The codes the trip page words (AA-957): every ``crm.task`` kind and every ``crm.trip_stage_history`` reason the API
writes.

The database keeps both as bare codes (crm-005 ``task_kind_chk``, crm-012 ``trip_stage_history_reason_chk``: no free
text), and the web turns each one into a sentence (``task.<kind>``, ``reason.<code>`` in ``web/lib/i18n*.ts``). A code
the web cannot word shows raw on the trip page ("stage3_due_t21", "deposit_paid -> Booked"). So this file is the list
both sides check against:

- ``tests/unit/test_task_kinds.py`` reads the API source and fails when a module writes a kind or a reason that is not
  listed here (or one listed here is no longer written);
- ``web/tests/tripCodes.test.ts`` reads this file and fails when a listed code has no EN or VI label.

Adding a task kind or a stage reason = one line here + its two labels in the web.
"""

from __future__ import annotations

# Every kind a module inserts into crm.task.
TASK_KINDS: frozenset[str] = frozenset(
    {
        "aa_cancel_failed",
        "aa_confirm_failed",
        "aa_hold_expired",
        "aa_incident",
        "aa_line_declined",
        "aa_lines_failed",
        "add_commission",
        "call_scheduled",
        "call_slot_taken",
        "cancel_refund_review",
        "cancel_with_suppliers",
        "collect_deposit",
        "commission_overdue",
        "date_rule_gate_unmet",
        "get_planning_info",
        "invoice_overdue",
        "ops_release_incomplete",
        "payment_unfulfillable",
        "proposal_change_request",
        "proposal_follow_up",
        "proposal_hold_problem",
        "reply_inquiry",
        "return_date_missing",
        "stage3_due_t21",
        "verify_transfer",
    }
)

# Every whole reason code a stage move writes into crm.trip_stage_history.
STAGE_REASONS: frozenset[str] = frozenset(
    {
        "agent_move",
        "bulk_move",
        "date_rule",
        "date_rule_gate_unmet",
        "deposit_paid",
        "import",
        "intake",
        "undo",
    }
)

# Reason families: ``lost_<lost reason>`` (app.pipeline.LOST_REASONS) and ``cancel_<cancel reason>``
# (app.stages.CANCEL_REASONS); the web words the family and labels the inner code (``cancel.<code>``).
STAGE_REASON_PREFIXES: tuple[str, ...] = ("lost_", "cancel_")
