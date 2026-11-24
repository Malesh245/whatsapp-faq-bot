"""End-to-end tests through the HTTP API with a scripted fake LLM (no API key needed).

The fake stands in for the model so we can test OUR logic: webhook parsing,
memory, guardrails, lead capture and handoff.
"""

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.bot import FALLBACK_REPLY, limit_words, numbers_in
from app.config import BASE_DIR, Settings
from app.main import create_app

PHONE = "919876543210"


class FakeLLM:
    """Returns canned decisions based on the latest customer message."""

    def __init__(self):
        self.calls = []  # (system_prompt, messages) for assertions

    def complete_json(self, system_prompt, messages):
        self.calls.append((system_prompt, messages))
        last = messages[-1]["content"].lower()
        history = " ".join(m["content"].lower() for m in messages[:-1])
        base = {
            "answer_found": True,
            "wants_human": False,
            "customer_name": None,
            "buying_intent": False,
            "product_interest": None,
        }

        if "boom" in last:
            raise TimeoutError("model unavailable")
        if "price of a4" in last:
            return {
                **base,
                "reply": "A4 copier paper (75 GSM, 500 sheets) is ₹260 per ream.",
                "product_interest": "A4 copier paper",
            }
        if "bigger size" in last:
            # A real model resolves this from history; check the fake got that history.
            assert (
                "a4" in history
            ), "follow-up must be answered with conversation history"
            return {
                **base,
                "reply": "The bigger A3 size (75 GSM, 500 sheets) is ₹540 per ream.",
                "product_interest": "A3 copier paper",
            }
        if "my name is" in last:
            return {
                **base,
                "reply": "Thanks Ravi! Our team will confirm your A3 order shortly.",
                "customer_name": "Ravi",
                "buying_intent": True,
                "product_interest": "A3 copier paper",
            }
        if "laptop" in last:
            return {**base, "reply": "", "answer_found": False}
        if "discount" in last:  # model "hallucinates" a number not in the FAQ
            return {**base, "reply": "Sure, you get 20% off today!"}
        if "talk to" in last:
            return {**base, "reply": "Sure!", "wants_human": True}
        if "long" in last:
            return {**base, "reply": "word " * 120}
        return {**base, "reply": "Hi! 👋 How can I help you today?"}


def wa_payload(text, msg_id, phone=PHONE, name="Ravi Kumar", msg_type="text"):
    """Minimal WhatsApp Cloud API 'messages' webhook body."""
    message = {"from": phone, "id": msg_id, "timestamp": "1764000000", "type": msg_type}
    if msg_type == "text":
        message["text"] = {"body": text}
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550000000",
                                "phone_number_id": "PNID",
                            },
                            "contacts": [{"profile": {"name": name}, "wa_id": phone}],
                            "messages": [message],
                        },
                    }
                ],
            }
        ],
    }


@pytest.fixture
def setup(tmp_path):
    settings = Settings(
        database_path=str(tmp_path / "test.db"),
        faq_path=str(BASE_DIR / "data" / "faq.md"),
        whatsapp_verify_token="secret-token",
    )
    llm = FakeLLM()
    client = TestClient(create_app(settings, llm=llm))
    return client, llm


def send(client, text, msg_id, **kw):
    resp = client.post("/webhook", json=wa_payload(text, msg_id, **kw))
    assert resp.status_code == 200
    return resp.json()["processed"][0]


# --- webhook --------------------------------------------------------------
def test_webhook_verification(setup):
    client, _ = setup
    ok = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "secret-token",
            "hub.challenge": "12345",
        },
    )
    assert ok.status_code == 200 and ok.text == "12345"
    bad = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong",
            "hub.challenge": "12345",
        },
    )
    assert bad.status_code == 403


def test_status_updates_are_acknowledged_and_ignored(setup):
    client, llm = setup
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "statuses": [
                                {
                                    "id": "wamid.X",
                                    "status": "delivered",
                                    "recipient_id": PHONE,
                                }
                            ]
                        }
                    }
                ]
            }
        ],
    }
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200 and resp.json()["processed"] == []
    assert llm.calls == []


def test_duplicate_webhook_is_answered_once(setup):
    client, llm = setup
    send(client, "hi", "wamid.1")
    again = send(client, "hi", "wamid.1")
    assert again["duplicate"] is True and again["reply"] is None
    assert len(llm.calls) == 1


def test_signature_is_checked_when_app_secret_set(tmp_path):
    settings = Settings(
        database_path=str(tmp_path / "t.db"),
        faq_path=str(BASE_DIR / "data" / "faq.md"),
        whatsapp_app_secret="appsecret",
    )
    client = TestClient(create_app(settings, llm=FakeLLM()))
    body = json.dumps(wa_payload("hi", "wamid.s")).encode()
    assert (
        client.post(
            "/webhook", content=body, headers={"X-Hub-Signature-256": "sha256=bad"}
        ).status_code
        == 403
    )
    sig = hmac.new(b"appsecret", body, hashlib.sha256).hexdigest()
    assert (
        client.post(
            "/webhook",
            content=body,
            headers={
                "X-Hub-Signature-256": f"sha256={sig}",
                "Content-Type": "application/json",
            },
        ).status_code
        == 200
    )


# --- AI replies + memory ----------------------------------------------------
def test_faq_answer_and_follow_up_uses_history(setup):
    client, llm = setup
    r1 = send(client, "What is the price of A4 paper?", "wamid.1")
    assert "₹260" in r1["reply"] and not r1["needs_human"]

    r2 = send(client, "What about the bigger size?", "wamid.2")
    assert "₹540" in r2["reply"]

    # The FAQ is in the system prompt and history was passed in order.
    system_prompt, messages = llm.calls[-1]
    assert "A3 copier paper" in system_prompt
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]

    convo = client.get(f"/conversations/{PHONE}").json()
    assert len(convo["messages"]) == 4 and convo["profile_name"] == "Ravi Kumar"


def test_history_is_separate_per_phone(setup):
    client, llm = setup
    send(client, "What is the price of A4 paper?", "wamid.1")
    send(client, "hello", "wamid.2", phone="918888888888")
    _, messages = llm.calls[-1]
    assert len(messages) == 1


def test_unknown_question_falls_back_and_flags_handoff(setup):
    client, _ = setup
    r = send(client, "Do you sell laptops?", "wamid.1")
    assert r["reply"] == FALLBACK_REPLY and r["needs_human"]
    handoffs = client.get("/handoffs").json()
    assert handoffs[0]["phone"] == PHONE
    assert handoffs[0]["handoff_reason"] == "answer_not_in_faq"
    assert handoffs[0]["last_customer_message"] == "Do you sell laptops?"


def test_invented_numbers_are_blocked(setup):
    client, _ = setup
    r = send(client, "any discount for me?", "wamid.1")
    assert r["reply"] == FALLBACK_REPLY
    assert r["handoff_reason"] == "unverified_facts_in_reply"


def test_customer_asks_for_person(setup):
    client, _ = setup
    r = send(client, "I want to talk to someone", "wamid.1")
    assert (
        r["reply"] == FALLBACK_REPLY
        and r["handoff_reason"] == "customer_requested_human"
    )


def test_keyword_backup_catches_human_request(setup):
    client, _ = setup  # fake LLM returns a normal greeting for this text
    r = send(client, "can a real person call me?", "wamid.1")
    assert r["needs_human"]


def test_llm_failure_is_safe(setup):
    client, _ = setup
    r = send(client, "boom", "wamid.1")
    assert r["reply"] == FALLBACK_REPLY and r["handoff_reason"] == "ai_error"


def test_non_text_message_goes_to_human(setup):
    client, _ = setup
    r = send(client, "", "wamid.1", msg_type="image")
    assert (
        r["reply"] == FALLBACK_REPLY
        and r["handoff_reason"] == "unsupported_message_type"
    )


def test_resolve_handoff(setup):
    client, _ = setup
    send(client, "Do you sell laptops?", "wamid.1")
    assert client.post(f"/handoffs/{PHONE}/resolve").status_code == 200
    assert client.get("/handoffs").json() == []


def test_reply_is_capped_at_80_words(setup):
    client, _ = setup
    r = send(client, "give me a long answer", "wamid.1")
    assert len(r["reply"].split()) <= 80


# --- leads ------------------------------------------------------------------
def test_lead_saved_with_name_and_product_from_context(setup):
    client, _ = setup
    send(client, "What is the price of A4 paper?", "wamid.1")
    send(client, "What about the bigger size?", "wamid.2")
    r = send(client, "My name is Ravi, I want to order 3 reams", "wamid.3")
    assert r["lead_saved"]
    lead = client.get("/leads").json()[0]
    assert lead["phone"] == PHONE
    assert lead["name"] == "Ravi"
    assert lead["product_interest"] == "A3 copier paper"
    assert lead["created_at"]


def test_no_lead_for_plain_questions(setup):
    client, _ = setup
    send(client, "hi", "wamid.1")
    assert client.get("/leads").json() == []


def test_keyword_backup_captures_buying_intent(setup):
    client, _ = setup  # fake LLM does not flag intent for this text
    send(client, "I want to buy paper", "wamid.1")
    lead = client.get("/leads").json()[0]
    assert lead["name"] == "Ravi Kumar"  # falls back to WhatsApp profile name


def test_admin_key_protects_staff_endpoints(tmp_path):
    settings = Settings(
        database_path=str(tmp_path / "t.db"),
        faq_path=str(BASE_DIR / "data" / "faq.md"),
        admin_api_key="k",
    )
    client = TestClient(create_app(settings, llm=FakeLLM()))
    assert client.get("/leads").status_code == 401
    assert client.get("/leads", headers={"X-API-Key": "k"}).status_code == 200


# --- helpers ----------------------------------------------------------------
def test_helpers():
    assert numbers_in("₹1,250.00 by 7:30") == {"1250", "7", "30"}
    assert limit_words("a b c", 2) == "a b…"
