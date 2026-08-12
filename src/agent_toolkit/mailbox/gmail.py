"""GmailMailbox — delegates to agent_toolkit.gmail_client (REST API).

Ported from ClaudeAIScoutMaster's app/mailbox/gmail.py (see
ClaudeAIScoutMaster#277). One thing carried over deliberately, not fixed:
`forward()` sends the forwarded copy via `OutlookMailbox` rather than Gmail
(Gmail has no forward-via-API that preserves Donna's own comment cleanly,
so the source hybridizes — fetch from Gmail, send via Outlook). That's a
real behavioral coupling from Donna's original setup, where she owns both a
Gmail account and the Outlook account. A consumer that owns Gmail without
also owning an Outlook mailbox (plausible for a future agent) needs its own
forward implementation — this one will raise/misbehave for such a consumer
if never registered against an OutlookMailbox. Flagged here rather than
silently redesigned, since Donna is still the near-term consumer.
"""
from __future__ import annotations

import logging

from googleapiclient.errors import HttpError

from agent_toolkit.mailbox.client import Email, MailActionError, MailboxClient

log = logging.getLogger(__name__)


def _run_mutation(op_name: str, msg_id: str, fn) -> None:
    """Run a read-state mutation, treating 404 (already gone) as success.

    Any other failure raises MailActionError so the caller (scheduler) knows
    not to mark this message as seen/handled — it needs to be retried.
    """
    try:
        fn()
    except HttpError as exc:
        if exc.resp.status == 404:
            log.info("GmailMailbox.%s(%s): already gone (404), treating as handled", op_name, msg_id)
            return
        log.error("GmailMailbox.%s(%s) failed: %s", op_name, msg_id, exc)
        raise MailActionError(f"{op_name} failed for {msg_id}") from exc
    except Exception as exc:
        log.error("GmailMailbox.%s(%s) failed: %s", op_name, msg_id, exc)
        raise MailActionError(f"{op_name} failed for {msg_id}") from exc


class GmailMailbox(MailboxClient):
    """Gmail mailbox backed by Gmail REST API via agent_toolkit.gmail_client."""

    def __init__(self, token_env: str, mailbox_name: str) -> None:
        self.token_env = token_env
        self.mailbox_name = mailbox_name

    # ------------------------------------------------------------------ #
    # ABC methods
    # ------------------------------------------------------------------ #

    def fetch_unread(self, limit: int = 10) -> list[Email]:
        from agent_toolkit import gmail_client
        raw = gmail_client.fetch_unread(limit=limit, token_env=self.token_env)
        return [
            Email(
                uid=m["uid"],
                subject=m["subject"],
                sender=m["sender"],
                sender_email=m["sender_email"],
                date=m["date"],
                body=m["body"],
                mailbox_name=self.mailbox_name,
                source_type="gmail",
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
        from agent_toolkit import gmail_client
        raw = gmail_client.search_messages(
            keyword=keyword, sender=sender, since=since, until=until,
            limit=limit, token_env=self.token_env,
        )
        return [
            Email(
                uid=m["uid"],
                subject=m["subject"],
                sender=m["sender"],
                sender_email=m["sender_email"],
                date=m["date"],
                body=m["body"],
                mailbox_name=self.mailbox_name,
                source_type="gmail",
                message_id=m.get("message_id", ""),
                thread_id=m.get("thread_id", ""),
            )
            for m in raw
        ]

    def mark_read(self, msg_id: str) -> None:
        from agent_toolkit import gmail_client
        _run_mutation("mark_read", msg_id, lambda: gmail_client.mark_read(msg_id, token_env=self.token_env))

    def archive(self, msg_id: str) -> None:
        from agent_toolkit import gmail_client
        _run_mutation("archive", msg_id, lambda: gmail_client.archive_message(msg_id, token_env=self.token_env))

    def trash(self, msg_id: str) -> None:
        from agent_toolkit import gmail_client
        _run_mutation("trash", msg_id, lambda: gmail_client.trash_message(msg_id, token_env=self.token_env))

    def file_to(self, msg_id: str, label_or_folder: str) -> None:
        from agent_toolkit import gmail_client
        try:
            gmail_client.apply_label(msg_id, label_or_folder, token_env=self.token_env)
        except Exception as exc:
            log.warning("GmailMailbox.file_to(%s, %s) failed: %s — falling back to archive", msg_id, label_or_folder, exc)
            self.archive(msg_id)

    def send_reply(self, to_address: str, subject: str, body: str, in_reply_to: str | None = None,
                   thread_id: str | None = None) -> None:
        from agent_toolkit import gmail_client
        reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        gmail_client.send_message(
            to_address=to_address,
            subject=reply_subject,
            body=body,
            token_env=self.token_env,
            in_reply_to=in_reply_to,
            thread_id=thread_id,
        )

    def save_draft_reply(self, to_address: str, subject: str, body: str,
                         thread_id: str | None = None, in_reply_to: str | None = None) -> str:
        from agent_toolkit import gmail_client
        return gmail_client.create_draft(
            to_address=to_address,
            subject=subject,
            body=body,
            token_env=self.token_env,
            thread_id=thread_id,
            in_reply_to=in_reply_to,
        )

    def save_draft_new(self, to_address: str, subject: str, body: str) -> str:
        from agent_toolkit import gmail_client
        return gmail_client.create_draft_new(
            to_address=to_address, subject=subject, body=body, token_env=self.token_env,
        )

    def save_quoted_reply_draft(self, source_msg_id: str, body_html: str) -> str:
        from agent_toolkit import gmail_client
        return gmail_client.create_quoted_reply_draft(
            source_msg_id=source_msg_id, body_html=body_html, token_env=self.token_env,
        )

    def send_saved_draft(self, draft_id: str) -> None:
        from agent_toolkit import gmail_client
        gmail_client.send_draft(draft_id, token_env=self.token_env)

    def update_saved_draft(self, draft_id: str, to_address: str, subject: str, body: str) -> None:
        from agent_toolkit import gmail_client
        gmail_client.update_draft(
            draft_id=draft_id, to_address=to_address, subject=subject, body=body,
            token_env=self.token_env,
        )

    def list_drafts(self, limit: int = 20) -> list[dict]:
        from agent_toolkit import gmail_client
        return gmail_client.list_drafts(limit=limit, token_env=self.token_env)

    def send_new(self, to_address: str, subject: str, body: str) -> None:
        from agent_toolkit import gmail_client
        gmail_client.send_message(
            to_address=to_address,
            subject=subject,
            body=body,
            token_env=self.token_env,
        )

    def forward(self, msg_id: str, to: str, comment: str = "") -> None:
        """Forward a Gmail message via Outlook (cross-account send) — see the
        module docstring for why this hybrid exists and its limitation.

        Fetches the original message from Gmail, composes a forwarded body,
        then sends via OutlookMailbox.send_new. Marks the original read and
        archives it.
        """
        from agent_toolkit import gmail_client
        from agent_toolkit.mailbox.outlook import OutlookMailbox

        # Fetch full message to get headers + body
        service = gmail_client._get_service(self.token_env)
        msg = service.users().messages().get(
            userId="me", id=msg_id, format="full"
        ).execute()

        headers = msg.get("payload", {}).get("headers", [])
        orig_from = gmail_client._get_header(headers, "From")
        orig_subject = gmail_client._get_header(headers, "Subject") or "(no subject)"
        orig_date = gmail_client._get_header(headers, "Date")
        orig_body = gmail_client._extract_body_from_payload(msg.get("payload", {}))

        fwd_subject = orig_subject if orig_subject.lower().startswith("fwd:") else f"Fwd: {orig_subject}"
        fwd_body_parts = []
        if comment:
            fwd_body_parts.append(comment)
            fwd_body_parts.append("")
        fwd_body_parts += [
            "---------- Forwarded message ----------",
            f"From: {orig_from}",
            f"Date: {orig_date}",
            f"Subject: {orig_subject}",
        ]
        # OutlookMailbox.send_new sends with contentType HTML, so this header
        # block would collapse into one line and the quoted original would
        # lose every paragraph break if sent as plain text. Convert the
        # header explicitly, and let the original body through as-is when it
        # already is HTML.
        from agent_toolkit.email_html import _plain_text_to_html, _ensure_email_html

        fwd_body = _plain_text_to_html("\n".join(fwd_body_parts)) + _ensure_email_html(orig_body)

        # Send via Outlook
        OutlookMailbox().send_new(
            to_address=to,
            subject=fwd_subject,
            body=fwd_body,
        )

        # Clean up the original
        self.mark_read(msg_id)
        self.archive(msg_id)
