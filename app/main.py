"""HTTP layer (FastAPI). Run with:

    uvicorn app.main:create_app --factory --reload
"""
import json
import logging
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from starlette.concurrency import run_in_threadpool

from app.bot import Bot
from app.config import Settings, load_settings
from app.db import Database
from app.llm import LLMClient, OpenAICompatibleLLM
from app.whatsapp import extract_messages, send_text_message, verify_signature

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def create_app(settings: Settings | None = None, llm: LLMClient | None = None) -> FastAPI:
    settings = settings or load_settings()
    db = Database(settings.database_path)
    faq_text = Path(settings.faq_path).read_text(encoding="utf-8")
    llm = llm or OpenAICompatibleLLM(
        api_key=settings.llm_api_key, model=settings.llm_model, base_url=settings.llm_base_url
    )
    bot = Bot(db, llm, faq_text, settings.history_limit)

    app = FastAPI(title="Reliable Paper Mart – WhatsApp FAQ Assistant", version="1.0.0")

    def require_admin(x_api_key: str | None = Header(default=None)):
        if settings.admin_api_key and x_api_key != settings.admin_api_key:
            raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    # --- WhatsApp webhook -------------------------------------------------
    @app.get("/webhook", response_class=PlainTextResponse)
    def verify_webhook(
        mode: str = Query(alias="hub.mode", default=""),
        token: str = Query(alias="hub.verify_token", default=""),
        challenge: str = Query(alias="hub.challenge", default=""),
    ):
        """One-time handshake Meta does when you register the webhook URL."""
        if mode == "subscribe" and token == settings.whatsapp_verify_token:
            return challenge
        raise HTTPException(status_code=403, detail="Verification failed")

    @app.post("/webhook")
    async def receive_webhook(request: Request):
        raw = await request.body()
        if settings.whatsapp_app_secret and not verify_signature(
            settings.whatsapp_app_secret, raw, request.headers.get("X-Hub-Signature-256")
        ):
            raise HTTPException(status_code=403, detail="Invalid signature")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Body must be JSON")

        processed = []
        for msg in extract_messages(payload):
            # The LLM call is blocking, so run it off the event loop.
            result = await run_in_threadpool(bot.handle, msg)
            if result.reply:
                await run_in_threadpool(send_text_message, settings, msg.phone, result.reply)
            processed.append(
                {
                    "phone": result.phone,
                    "reply": result.reply,
                    "needs_human": result.needs_human,
                    "handoff_reason": result.handoff_reason,
                    "lead_saved": result.lead is not None,
                    "duplicate": result.duplicate,
                }
            )
        # Meta only needs a 200; the body is there so curl/Postman shows the reply.
        return {"status": "ok", "processed": processed}

    # --- staff / admin endpoints ------------------------------------------
    @app.get("/handoffs", dependencies=[Depends(require_admin)])
    def list_handoffs():
        """Conversations flagged 'needs human'."""
        return db.list_handoffs()

    @app.post("/handoffs/{phone}/resolve", dependencies=[Depends(require_admin)])
    def resolve_handoff(phone: str):
        if not db.resolve_handoff(phone):
            raise HTTPException(status_code=404, detail="Conversation not found")
        return {"phone": phone, "needs_human": False}

    @app.get("/leads", dependencies=[Depends(require_admin)])
    def list_leads():
        return db.list_leads()

    @app.get("/conversations/{phone}", dependencies=[Depends(require_admin)])
    def get_conversation(phone: str, limit: int = 50):
        conversation = db.get_conversation(phone)
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")
        conversation["messages"] = db.recent_messages(phone, limit)
        return conversation

    return app
