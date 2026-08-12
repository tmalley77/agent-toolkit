"""Single-user Telegram identity gate + high-consequence tool audit log.

Ported from ClaudeAIScoutMaster's app/auth/telegram_gate.py (see
ClaudeAIScoutMaster#277). Generalized from Donna's original hardcoded
"donna.audit" logger name and fixed HIGH_CONSEQUENCE_TOOLS set: each
consumer now supplies its own logger name and tool set via
`make_audit_log()`, since Gretchen's high-consequence tools (roster edits,
TeamSnap writes, ...) differ from Donna's (email sends, triage actions, ...).

Checks the Telegram numeric sender identity (effective_user.id / "from.id"),
which Telegram authenticates and the Bot API can't forge — not chat_id
(scoped to the chat, not the sender) and not username (self-reported,
changeable).

Fails closed: unset/empty config, a missing sender, or any other sender
id all deny.
"""
import logging
import os
from datetime import datetime, timezone
from typing import Callable, Iterable


def is_allowed_sender(update, env_var: str = "TELEGRAM_ALLOWED_USER_ID") -> bool:
    """True only for the sender id configured in `env_var`."""
    allowed = os.getenv(env_var, "")
    if not allowed:
        return False
    user = getattr(update, "effective_user", None)
    if user is None:
        return False
    return str(user.id) == allowed


def make_audit_log(logger_name: str, high_consequence_tools: Iterable[str]) -> Callable[[object, str], None]:
    """Build an `audit_log(update, tool_name)` bound to this agent's own
    logger name and high-consequence tool set.

    Writes one audit line (sender_id + tool + timestamp) only for tool
    names in `high_consequence_tools` — this is a standing security log
    for send/publish/delete-shaped actions, not general tool-call tracing.
    No-op for every other tool name.
    """
    audit_logger = logging.getLogger(logger_name)
    tools = frozenset(high_consequence_tools)

    def audit_log(update, tool_name: str) -> None:
        if tool_name not in tools:
            return
        user = getattr(update, "effective_user", None)
        sender_id = str(user.id) if user is not None else "unknown"
        audit_logger.warning(
            "AUDIT sender_id=%s tool=%s timestamp=%s",
            sender_id, tool_name, datetime.now(timezone.utc).isoformat(),
        )

    return audit_log
