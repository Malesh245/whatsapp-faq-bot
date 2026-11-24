"""Thin wrapper around an OpenAI-compatible chat API that returns parsed JSON.

The same code works with OpenAI, Google Gemini, Groq or a local Ollama model:
only LLM_BASE_URL, LLM_MODEL and LLM_API_KEY change.
"""
import json
import re
from typing import Protocol

from openai import OpenAI


class LLMClient(Protocol):
    def complete_json(self, system_prompt: str, messages: list[dict]) -> dict: ...


class OpenAICompatibleLLM:
    def __init__(self, api_key: str, model: str, base_url: str | None = None, timeout: float = 20.0):
        if not api_key:
            raise RuntimeError("LLM_API_KEY is not set. Copy .env.example to .env and add a key.")
        self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=1)
        self.model = model

    def complete_json(self, system_prompt: str, messages: list[dict]) -> dict:
        response = self.client.chat.completions.create(
            model=self.model,
            temperature=0,  # factual answers, not creative ones
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system_prompt}, *messages],
        )
        return parse_json_object(response.choices[0].message.content or "")


def parse_json_object(text: str) -> dict:
    """Parse the model output, tolerating ```json fences or stray text around it."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError(f"LLM did not return JSON: {text[:200]!r}")
        return json.loads(match.group(0))
