"""Everything specific to the WhatsApp Cloud API: parsing webhooks, signatures, sending."""

import hashlib
import hmac
import logging
from dataclasses import dataclass

import httpx

from app.config import Settings

log = logging.getLogger(__name__)


@dataclass
class IncomingMessage:
    wa_message_id: str
    phone: str  # sender's wa_id, e.g. "919876543210"
    text: str  # "" for media / unsupported types
    msg_type: str
    profile_name: str | None = None


def extract_messages(payload: dict) -> list[IncomingMessage]:
    """Pull customer messages out of a Cloud API webhook payload.

    Shape: entry[].changes[].value.{contacts[], messages[]}. The same webhook also
    delivers delivery/read `statuses`; those have no `messages` and are skipped.
    """
    if payload.get("object") != "whatsapp_business_account":
        return []

    found: list[IncomingMessage] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {
                c.get("wa_id"): (c.get("profile") or {}).get("name")
                for c in value.get("contacts", [])
            }
            for m in value.get("messages", []):
                msg_type = m.get("type", "unknown")
                found.append(
                    IncomingMessage(
                        wa_message_id=m.get("id", ""),
                        phone=m.get("from", ""),
                        text=_message_text(m, msg_type).strip(),
                        msg_type=msg_type,
                        profile_name=names.get(m.get("from")),
                    )
                )
    return [m for m in found if m.phone]


def _message_text(m: dict, msg_type: str) -> str:
    if msg_type == "text":
        return (m.get("text") or {}).get("body", "")
    if msg_type == "button":  # quick-reply button on a template
        return (m.get("button") or {}).get("text", "")
    if msg_type == "interactive":  # reply buttons / list picks
        inter = m.get("interactive") or {}
        picked = inter.get("button_reply") or inter.get("list_reply") or {}
        return picked.get("title", "")
    return ""  # image, audio, document, location, sticker...


def verify_signature(app_secret: str, raw_body: bytes, header: str | None) -> bool:
    """Check Meta's X-Hub-Signature-256 header (HMAC-SHA256 of the raw body)."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def send_text_message(settings: Settings, to: str, text: str) -> bool:
    """Send a reply through the Cloud API. Without credentials we only log it,
    which is how the prototype runs with simulated requests."""
    if not (settings.whatsapp_access_token and settings.whatsapp_phone_number_id):
        log.info("[simulated send] to=%s: %s", to, text)
        return False

    url = (
        f"https://graph.facebook.com/{settings.graph_api_version}/"
        f"{settings.whatsapp_phone_number_id}/messages"
    )
    body = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }
    try:
        resp = httpx.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        log.error("Failed to send WhatsApp message to %s: %s", to, exc)
        return False
