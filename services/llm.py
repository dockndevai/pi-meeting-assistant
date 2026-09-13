"""
services/llm.py

OpenRouter — single endpoint, OpenAI-compatible API, routes to any model.

Decision: OpenRouter over direct Gemini/Anthropic API
  One base URL, one auth format, model is just a string you swap in .env.
  If google/gemini-3.1-flash-lite has an outage or quota issue, change
  one line in .env to switch models — no code change needed.
  OpenRouter also adds automatic fallback routing on its end.

Decision: OpenAI function calling format (type: "function" wrapper)
  OpenRouter follows the OpenAI spec exactly. Tools are wrapped as
  {"type": "function", "function": {...}} and responses come back as
  choices[0].message.tool_calls[]. This is the most widely supported
  format across all models OpenRouter proxies.

Decision: tool_choice="required" instead of "auto"
  "auto" lets the model decide whether to call a function or respond
  in text. For this task we always need function calls — "required"
  forces at least one tool call every response, same rationale as
  Gemini's mode=ANY.

Decision: temperature=0.1
  Low temperature = deterministic, structured output. We don't want
  creative variation in work item titles or summaries — consistency
  matters more than variety here. Especially important on a Pi where
  you can't easily retry a bad response.
"""

import json
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

# ── Load environment variables ───────────────────────────────────────────────
load_dotenv()

# ── Load config once at startup ───────────────────────────────────────────────
_config_dir   = Path(__file__).parent.parent / "config"
TOOLS         = json.loads((_config_dir / "tools.json").read_text())
SYSTEM_PROMPT = (_config_dir / "prompt.md").read_text()

# ── OpenRouter config ─────────────────────────────────────────────────────────
_BASE_URL = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1")
_MODEL    = os.environ.get("LLM_MODEL", "google/gemini-3.1-flash-lite")   # overridable via LLM_MODEL env var

# ── Shared async HTTP client ──────────────────────────────────────────────────
_client = httpx.AsyncClient(
    base_url=_BASE_URL,
    timeout=httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=5.0),
    headers={"content-type": "application/json"},
)


async def run_llm(transcript: str) -> dict:
    """
    Send transcript to OpenRouter with OpenAI-compatible function calling.
    Returns {"summary": {...}, "work_items": [...]}
    """
    api_key = os.environ.get("LLM_API_KEY", "")
    if not api_key:
        raise ValueError("LLM_API_KEY is not set")

    model = os.environ.get("LLM_MODEL", _MODEL)

    payload = {
        "model": model,
        "temperature": 0.1,
        "max_tokens": 1024,
        "tools": TOOLS,
        # "required" forces at least one tool call — no plain-text escape hatch
        "tool_choice": "required",
        "messages": [
            {
                "role":    "system",
                "content": SYSTEM_PROMPT,
            },
            {
                "role":    "user",
                "content": (
                    "Here is the meeting transcript:\n\n"
                    f"<transcript>\n{transcript}\n</transcript>\n\n"
                    "Please summarise the meeting and extract all action items."
                ),
            },
        ],
    }

    response = await _client.post(
        "/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {api_key}"},
    )

    if response.status_code != 200:
        raise RuntimeError(f"OpenRouter API {response.status_code}: {response.text}")

    data       = response.json()
    summary    = None
    work_items = []

    # OpenAI format: choices[0].message.tool_calls[]
    tool_calls = (
        data.get("choices", [{}])[0]
            .get("message", {})
            .get("tool_calls", [])
    )

    for call in tool_calls:
        name = call.get("function", {}).get("name")
        # arguments is a JSON string — parse it
        raw_args = call.get("function", {}).get("arguments", "{}")
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Failed to parse tool arguments for {name}: {e}")

        if name == "send_channel_message":
            summary = args
        elif name == "wit_create_work_item":
            work_items.append(args)

    if summary is None:
        raise RuntimeError("Model did not call send_channel_message — check your prompt.md")

    return {"summary": summary, "work_items": work_items}


async def run_chat(message: str, context: str = None) -> str:
    """
    Send user chat message and optional meeting context to OpenRouter.
    Returns the textual response from the model.
    """
    api_key = os.environ.get("LLM_API_KEY", "")
    if not api_key:
        return "Error: LLM_API_KEY is not set in backend environment. Please set it in the .env file."

    model = os.environ.get("LLM_MODEL", _MODEL)

    system_msg = (
        "You are PMBot's intelligent AI Meeting Assistant.\n"
        "You help users query their meeting transcripts, summary, decisions, and action items, "
        "and draft new items or answer general questions.\n"
        "Keep your answers helpful, clean, and formatted with markdown."
    )
    if context:
        system_msg += f"\n\nHere is the current meeting context for reference:\n{context}"

    payload = {
        "model": model,
        "temperature": 0.7,
        "max_tokens": 800,
        "messages": [
            {
                "role": "system",
                "content": system_msg,
            },
            {
                "role": "user",
                "content": message,
            },
        ],
    }

    try:
        response = await _client.post(
            "/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        if response.status_code != 200:
            return f"Error: OpenRouter API returned status code {response.status_code} - {response.text}"

        data = response.json()
        return (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "No response content from AI.")
        )
    except Exception as e:
        return f"Error contacting AI Agent model: {str(e)}"

