"""
services/teams.py

Decision: reuse the same httpx.AsyncClient as llm.py? No — separate client.
  Teams webhook has a different base URL and different timeout requirements.
  A Teams webhook responds in ~200-500ms (fast, reliable Microsoft infra).
  Separate client with a tighter timeout catches failures faster.
"""

import os
import httpx

_client = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=10.0, read=15.0, write=10.0, pool=5.0),
)


async def post_teams_summary(payload: dict) -> dict:
    webhook_url = os.environ.get("TEAMS_WEBHOOK_URL", "")
    if not webhook_url:
        raise ValueError("TEAMS_WEBHOOK_URL is not set")

    title        = payload.get("title", "Meeting Summary")
    summary      = payload.get("summary", "")
    attendees    = payload.get("attendees", [])
    decisions    = payload.get("decisions", [])
    next_meeting = payload.get("next_meeting")

    # Decision: Adaptive Card over plain text webhook
    #   Adaptive Cards render with proper formatting on all Teams clients
    #   (desktop, mobile, web). A plain text message would lose structure.
    card = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type":    "AdaptiveCard",
                "version": "1.4",
                "body": [
                    {"type": "TextBlock", "text": f"📋 {title}",
                     "weight": "Bolder", "size": "Large", "wrap": True},
                    {"type": "TextBlock", "text": summary,
                     "wrap": True, "spacing": "Medium"},
                    *( [
                        {"type": "TextBlock", "text": "Decisions",
                         "weight": "Bolder", "spacing": "Medium"},
                        *[{"type": "TextBlock", "text": f"• {d}",
                           "wrap": True, "spacing": "Small"} for d in decisions],
                    ] if decisions else [] ),
                    *( [{"type": "FactSet",
                         "facts": [{"title": "Attendees", "value": ", ".join(attendees)}],
                         "spacing": "Medium"}]
                       if attendees else [] ),
                    *( [{"type": "FactSet",
                         "facts": [{"title": "Next meeting", "value": next_meeting}]}]
                       if next_meeting else [] ),
                ],
                "msteams": {"width": "Full"},
            },
        }],
    }

    res = await _client.post(webhook_url, json=card)
    if res.status_code not in (200, 202):
        raise RuntimeError(f"Teams webhook {res.status_code}: {res.text}")

    print("[teams] Summary posted successfully")
    return {"ok": True}
