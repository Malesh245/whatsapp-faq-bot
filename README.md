# Reliable Paper Mart – WhatsApp FAQ Assistant

A backend service that receives WhatsApp messages (Cloud API webhook format), answers them with an LLM **using only the business FAQ**, remembers each customer's conversation, saves sales leads, and flags conversations that need a person.

> **FAQ note:** the assignment email didn't include the FAQ, so `data/faq.md` holds sample data. Replace that file with the official FAQ; no code changes are needed.

## How it works

```
WhatsApp / curl ──POST /webhook──▶ whatsapp.py  parse payload, skip status updates, verify signature
                                        │
                                        ▼
                                    bot.py  ── 1. ignore duplicate message ids (WhatsApp retries)
                                        │     2. save message, load last 10 messages (memory)
                                        │     3. LLM → JSON decision (reply, answer_found, wants_human, lead info)
                                        │     4. guardrails in code → fixed fallback + "needs human" flag
                                        │     5. save lead, save reply
                                        ▼
                                    db.py (SQLite: conversations, messages, leads)
                                        │
                     reply ◀────────────┘  sent via Graph API if credentials set, else logged
```

| File | Responsibility |
|---|---|
| `app/main.py` | FastAPI routes (webhook + staff endpoints) |
| `app/whatsapp.py` | Webhook parsing, signature check, sending replies |
| `app/bot.py` | Prompt, guardrails, memory, lead and handoff logic |
| `app/llm.py` | Calls any OpenAI-compatible LLM and parses JSON |
| `app/db.py` | SQLite schema and queries |
| `data/faq.md` | Business knowledge (the only source of answers) |

## Setup

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                     # then add LLM_API_KEY
uvicorn app.main:create_app --factory --reload           # http://localhost:8000/docs
```

Any OpenAI-compatible provider works. Set `LLM_BASE_URL` and `LLM_MODEL` in `.env` for Gemini, Groq (both have free tiers) or a local Ollama model. Run commands from the project root, since paths in `.env` are relative to it.

## Try it

```bash
# Scripted demo conversation (FAQ answers, follow-up, handoff, lead)
python scripts/simulate.py

# Chat yourself
python scripts/simulate.py --interactive

# Raw webhook with curl
curl -X POST localhost:8000/webhook -H "Content-Type: application/json" -d @scripts/sample_message.json

# Staff views
curl localhost:8000/handoffs                          # conversations needing a human
curl localhost:8000/leads
curl localhost:8000/conversations/919876543210
curl -X POST localhost:8000/handoffs/919876543210/resolve
```

Meta only needs a `200` from the webhook. The response body also includes the reply and flags so simulated requests show what happened.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/webhook` | Meta's verification handshake (`hub.verify_token` → returns `hub.challenge`) |
| `POST` | `/webhook` | Incoming WhatsApp messages |
| `GET` | `/handoffs` | Conversations flagged **needs human**, with reason and last message |
| `POST` | `/handoffs/{phone}/resolve` | Staff clears the flag after helping |
| `GET` | `/leads` | Saved leads |
| `GET` | `/conversations/{phone}` | Conversation status and message history |
| `GET` | `/health` | Liveness check |

If `ADMIN_API_KEY` is set, staff endpoints need an `X-API-Key` header.

## How each requirement is met

**1. Webhook endpoint.** `POST /webhook` reads `entry[].changes[].value.messages[]`, the Cloud API shape. Delivery/read `statuses` are acknowledged and ignored. Text, button and interactive replies are read. Images and voice notes go to a person. The `GET /webhook` verification handshake and `X-Hub-Signature-256` check (when `WHATSAPP_APP_SECRET` is set) make it ready for a real number.

**2. AI replies from the FAQ only.** The full FAQ goes into the system prompt, which is fine for a small FAQ (see *Next steps* for larger ones). The model returns JSON at temperature 0. Its output isn't trusted blindly; `bot.py` enforces:
- `answer_found: false` → reply is exactly *"Let me connect you with our team member."*
- **Number check:** every number in the reply (price, time, days) must appear in the FAQ or in the customer's own messages. A made-up "20% off" or wrong price is replaced with the fallback. This catches the most damaging kind of hallucination for a shop.
- Replies are capped at 80 words in code (the prompt asks for under 60).
- If the LLM errors or returns bad JSON, the customer gets the fallback and staff are alerted, instead of the service crashing.

**3. Conversation memory.** Every message (customer and bot) is stored in `messages`, keyed by phone. The last `HISTORY_LIMIT` (10) are sent to the model as chat turns, so "What about the bigger size?" after an A4 question resolves to A3. Customer messages are saved *before* the LLM call, so nothing is lost on failure.

**4. Lead capture.** The model extracts `customer_name`, `buying_intent` and `product_interest`, using history so "I'll take that one" maps to the right product. Regex backups catch "my name is …" and "I want to order/buy …" if the model misses them. There is one lead row per phone, enriched over time: a name given later fills in an earlier lead, and known values are never overwritten with empty ones. If there's buying intent but no stated name, the WhatsApp profile name is used.

**5. Human handoff.** `needs_human` is set with a reason: `answer_not_in_faq`, `customer_requested_human`, `unverified_facts_in_reply`, `ai_error` or `unsupported_message_type`. Requests for a person are detected by the model *and* by a keyword backup. `GET /handoffs` lists flagged conversations, oldest first.

## Design choices

- **FastAPI + SQLite + plain SQL.** Small, no setup, easy to inspect (`sqlite3 data/bot.db "select * from leads"`). Moving to Postgres means changing only `db.py`.
- **Structured JSON output.** One LLM call returns the reply *and* the flags, so a single request does answer + lead extraction + handoff detection.
- **Guardrails in code, not just the prompt.** Prompts reduce hallucination; code checks guarantee the fallback text and catch invented numbers.
- **Idempotency.** WhatsApp retries webhooks, so `wa_message_id` is unique and a retried message is answered once.
- **Provider-agnostic LLM.** The OpenAI SDK with a configurable `base_url` works with OpenAI, Gemini, Groq and Ollama.

## Tests

```bash
pytest -q
```

19 tests run against the real HTTP API and database, with a scripted fake LLM (no API key needed). They cover webhook verification, status updates, duplicates, signatures, FAQ answers, follow-ups with history, per-phone memory, fallback and handoff, invented-number blocking, LLM failure, non-text messages, the word cap, lead capture and the admin key.

## Limitations and next steps

- The LLM is called inside the webhook request (about 1–3 s). At scale, return 200 immediately and process from a queue (Celery/RQ) to stay inside Meta's timeout.
- The bot keeps answering FAQ questions after a handoff. A "pause bot while staff reply" mode is a small addition.
- For a large catalogue, replace "whole FAQ in prompt" with retrieval (embeddings + top-k FAQ chunks).
- Hindi/Hinglish messages work through the LLM, but the keyword backups are English only.
- WhatsApp only allows free-form replies within 24 hours of the customer's last message. Later follow-ups need approved templates.
