"""Email delivery (plan section 5.11) via Resend.

A thin wrapper over the Resend HTTP API. The transport is injectable so tests
never make a network call. Nothing is sent unless an API key, sender, and
recipient are all configured in ``.env``.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from .config import Settings, get_settings

logger = logging.getLogger(__name__)

RESEND_ENDPOINT = "https://api.resend.com/emails"


class EmailError(RuntimeError):
    """Raised when an email cannot be delivered."""


def _default_transport(method: str, url: str, *, headers: dict, json: dict) -> Any:
    import httpx

    return httpx.request(method, url, headers=headers, json=json, timeout=30.0)


class ResendEmailer:
    def __init__(
        self,
        api_key: str,
        sender: str,
        to: str,
        *,
        transport: Callable[..., Any] | None = None,
    ) -> None:
        self.api_key = api_key
        self.sender = sender
        self.to = to
        self._transport = transport or _default_transport

    def is_configured(self) -> bool:
        return bool(self.api_key and self.sender and self.to)

    def send(self, subject: str, html: str, text: str | None = None) -> bool:
        if not self.is_configured():
            raise EmailError(
                "Email is not configured. Set RESEND_API_KEY, RESEND_FROM_EMAIL and "
                "RESEND_TO_EMAIL in .env."
            )
        payload: dict[str, Any] = {
            "from": self.sender,
            "to": [self.to],
            "subject": subject,
            "html": html,
        }
        if text:
            payload["text"] = text
        response = self._transport(
            "POST",
            RESEND_ENDPOINT,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        status = getattr(response, "status_code", 200)
        if status >= 400:
            body = getattr(response, "text", "")
            raise EmailError(f"Resend returned {status}: {body[:200]}")
        logger.info("Digest email sent to %s", self.to)
        return True


def send_digest(digest, settings: Settings | None = None, *, emailer: ResendEmailer | None = None) -> bool:
    from .digest import render_html, render_text

    settings = settings or get_settings()
    if emailer is None:
        emailer = ResendEmailer(
            api_key=settings.secrets.resend_api_key,
            sender=settings.secrets.resend_from_email,
            to=settings.secrets.resend_to_email,
        )
    return emailer.send(digest.subject, render_html(digest), render_text(digest))
