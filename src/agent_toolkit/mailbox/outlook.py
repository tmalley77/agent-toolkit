"""OutlookMailbox — delegates to agent_toolkit.outlook_client (Graph API)."""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from agent_toolkit.mailbox.client import Email, MailActionError, MailboxClient

log = logging.getLogger(__name__)


def _run_mutation(op_name: str, msg_id: str, fn) -> None:
    """Run a read-state mutation, treating 404 (already gone) as success.

    Any other failure raises MailActionError so the caller (scheduler) knows
    not to mark this message as seen/handled — it needs to be retried.

    Catches httpx.HTTPStatusError, not requests.exceptions.HTTPError — the
    source this was ported from (ClaudeAIScoutMaster's app/mailbox/outlook.py)
    checked the wrong exception type: outlook_client raises via httpx
    throughout, so the requests-based check never actually matched and every
    404 fell through to the generic Exception branch as a hard failure. See
    ClaudeAIScoutMaster#278.
    """
    try:
        fn()
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            log.info("OutlookMailbox.%s(%s): already gone (404), treating as handled", op_name, msg_id)
            return
        log.error("OutlookMailbox.%s(%s) failed: %s", op_name, msg_id, exc)
        raise MailActionError(f"{op_name} failed for {msg_id}") from exc
    except Exception as exc:
        log.error("OutlookMailbox.%s(%s) failed: %s", op_name, msg_id, exc)
        raise MailActionError(f"{op_name} failed for {msg_id}") from exc


class OutlookMailbox(MailboxClient):
    """Outlook mailbox backed by Graph API via agent_toolkit.outlook_client."""

    # ------------------------------------------------------------------ #
    # ABC methods
    # ------------------------------------------------------------------ #

    def fetch_unread(self, limit: int = 10) -> list[Email]:
        from agent_toolkit import outlook_client
        raw = outlook_client.fetch_unread(limit=limit)
        return [
            Email(
                uid=m["uid"],
                subject=m["subject"],
                sender=m["sender"],
                sender_email=m["sender_email"],
                date=m["date"],
                body=m["body"],
                mailbox_name="outlook",
                source_type="outlook",
                message_id=m.get("message_id", ""),
                thread_id=m.get("thread_id", ""),
            )
            for m in raw
        ]

    def search(
        self,
        keyword: str,
        sender: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 15,
    ) -> list[Email]:
        from agent_toolkit import outlook_client
        raw = outlook_client.search_messages(
            keyword=keyword, sender=sender, since=since, until=until, limit=limit,
        )
        return [
            Email(
                uid=m["uid"],
                subject=m["subject"],
                sender=m["sender"],
                sender_email=m["sender_email"],
                date=m["date"],
                body=m["body"],
                mailbox_name="outlook",
                source_type="outlook",
                message_id=m.get("message_id", ""),
                thread_id=m.get("thread_id", ""),
            )
            for m in raw
        ]

    def mark_read(self, msg_id: str) -> None:
        from agent_toolkit import outlook_client
        _run_mutation("mark_read", msg_id, lambda: outlook_client.mark_read(msg_id))

    def archive(self, msg_id: str) -> None:
        from agent_toolkit import outlook_client
        _run_mutation("archive", msg_id, lambda: outlook_client.archive_message(msg_id))

    def trash(self, msg_id: str) -> None:
        # Outlook doesn't have a separate trash from archive in the client;
        # archive_message falls back to deletedItems if Archive folder absent.
        self.archive(msg_id)

    def forward(self, msg_id: str, to: str, comment: str = "") -> None:
        from agent_toolkit import outlook_client
        outlook_client.forward_message(msg_id, to_address=to, comment=comment)

    def file_to(self, msg_id: str, label_or_folder: str) -> None:
        from agent_toolkit import outlook_client
        try:
            outlook_client.move_to_folder(msg_id, label_or_folder)
        except Exception as exc:
            log.warning("OutlookMailbox.file_to(%s, %s) failed: %s — falling back to archive", msg_id, label_or_folder, exc)
            self.archive(msg_id)

    # ------------------------------------------------------------------ #
    # Outlook-specific extras (not in ABC)
    # ------------------------------------------------------------------ #

    def send_reply(
        self,
        to_address: str,
        subject: str,
        body: str,
        in_reply_to: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> None:
        # thread_id accepted for interface parity; Graph threads on
        # conversationId server-side, so there is nothing to pass through.
        from agent_toolkit import outlook_client
        outlook_client.send_reply(
            to_address=to_address,
            subject=subject,
            body=body,
            in_reply_to=in_reply_to,
        )

    def save_draft_reply(
        self,
        to_address: str,
        subject: str,
        body: str,
    ) -> str:
        from agent_toolkit import outlook_client
        return outlook_client.create_draft_reply(
            to_address=to_address,
            subject=subject,
            body=body,
        )

    def save_draft_new(self, to_address: str, subject: str, body: str) -> str:
        from agent_toolkit import outlook_client
        return outlook_client.create_draft_new(
            to_address=to_address, subject=subject, body=body,
        )

    def save_quoted_reply_draft(self, source_msg_id: str, body_html: str) -> str:
        from agent_toolkit import outlook_client
        draft_id, _quoted = outlook_client.create_reply_draft(source_msg_id)
        outlook_client.prepend_to_draft_body(draft_id, body_html)
        return draft_id

    def send_saved_draft(self, draft_id: str) -> None:
        from agent_toolkit import outlook_client
        outlook_client.send_draft(draft_id)

    def update_saved_draft(self, draft_id: str, to_address: str, subject: str, body: str) -> None:
        from agent_toolkit import outlook_client
        outlook_client.update_draft(
            message_id=draft_id, to_address=to_address, subject=subject, body=body,
        )

    def list_drafts(self, limit: int = 20) -> list[dict]:
        from agent_toolkit import outlook_client
        return outlook_client.list_drafts(limit=limit)

    def send_new(
        self,
        to_address: str,
        subject: str,
        body: str,
        attachment_path: Optional[str] = None,
        attachment_name: Optional[str] = None,
    ) -> None:
        from agent_toolkit import outlook_client
        outlook_client.send_new_email(
            to_address=to_address,
            subject=subject,
            body=body,
            attachment_path=attachment_path,
            attachment_name=attachment_name,
        )

    def list_sent(self, since, limit: int = 200) -> list[dict]:
        from agent_toolkit import outlook_client
        return outlook_client.list_sent_messages(since=since, limit=limit)
