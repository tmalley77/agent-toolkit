"""Unified mailbox abstraction — ABC + registry-based factory.

Ported from ClaudeAIScoutMaster's app/mailbox/client.py (see
ClaudeAIScoutMaster#277). One behavioral change from the source: the
original factory hardcoded three mailbox names ("outlook", "troop_gmail",
"personal_gmail") specific to Donna's pre-split setup. This package is
meant to be reusable across agents, so `get_mailbox()` is registry-based
instead — each consumer calls `register_mailbox(name, factory)` for the
mailbox names it actually owns.

Usage:
    from agent_toolkit.mailbox import MailboxClient, Email, get_mailbox, register_mailbox
    from agent_toolkit.mailbox.outlook import OutlookMailbox

    register_mailbox("outlook", OutlookMailbox)
    mb = get_mailbox("outlook")
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable


class MailActionError(Exception):
    """A read-state mutation (mark_read/archive/trash) genuinely failed.

    Distinct from "already gone" (404), which implementations treat as
    success. Callers should NOT record the message as seen/handled when
    this is raised — leave it unseen so it's retried on the next check.
    """


@dataclass
class Email:
    uid: str
    subject: str
    sender: str
    sender_email: str
    date: str
    body: str
    mailbox_name: str
    source_type: str
    message_id: str = ""
    # Provider conversation id — Gmail's threadId, Graph's conversationId.
    # Triage groups unread mail on this so one conversation yields one
    # decision instead of one per message. Empty string degrades to
    # per-message behaviour.
    thread_id: str = ""


class MailboxClient(ABC):
    """Abstract base class for all mailbox implementations."""

    @abstractmethod
    def fetch_unread(self, limit: int = 10) -> list[Email]:
        """Fetch up to `limit` unread emails."""
        ...

    @abstractmethod
    def search(
        self,
        keyword: str,
        sender: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 15,
    ) -> list[Email]:
        """Search Inbox + Archive/All Mail + Sent (not Trash/Spam) by keyword,
        newest-first. `since`/`until` are "YYYY-MM-DD" strings."""
        ...

    @abstractmethod
    def mark_read(self, msg_id: str) -> None:
        """Mark a message as read."""
        ...

    @abstractmethod
    def archive(self, msg_id: str) -> None:
        """Archive a message (move out of inbox)."""
        ...

    @abstractmethod
    def trash(self, msg_id: str) -> None:
        """Move a message to trash."""
        ...

    def file_to(self, msg_id: str, label_or_folder: str) -> None:
        """File a message to a label (Gmail) or folder (Outlook). Default: archive."""
        self.archive(msg_id)

    @abstractmethod
    def forward(self, msg_id: str, to: str, comment: str = "") -> None:
        """Forward a message to another address."""
        ...

    @abstractmethod
    def send_reply(self, to_address: str, subject: str, body: str, in_reply_to: str | None = None,
                   thread_id: str | None = None) -> None:
        """Send a reply email from this mailbox."""
        ...

    @abstractmethod
    def send_new(self, to_address: str, subject: str, body: str) -> None:
        """Send a new email from this mailbox."""
        ...

    @abstractmethod
    def list_drafts(self, limit: int = 20) -> list[dict]:
        """List up to `limit` drafts sitting unsent in this mailbox's Drafts
        folder, newest-modified first. Returns [{"to","subject","last_modified"}]."""
        ...

    @abstractmethod
    def save_quoted_reply_draft(self, source_msg_id: str, body_html: str) -> str:
        """Save a reply draft to `source_msg_id` with `body_html` on top and the
        original quoted *by the provider's own rules*, formatting intact.
        `body_html` must already be HTML.
        """
        ...


_REGISTRY: dict[str, Callable[[], MailboxClient]] = {}


def register_mailbox(name: str, factory: Callable[[], MailboxClient]) -> None:
    """Register a mailbox name with the factory that builds it (e.g. a
    `MailboxClient` subclass itself, or a lambda closing over per-mailbox
    config like a token env var)."""
    _REGISTRY[name] = factory


def get_mailbox(name: str) -> MailboxClient:
    """Factory — returns the appropriate MailboxClient for the given
    registered name. Raises ValueError if nothing registered it."""
    if name not in _REGISTRY:
        raise ValueError(f"Unknown mailbox name: {name!r}. Registered: {sorted(_REGISTRY)}")
    return _REGISTRY[name]()
