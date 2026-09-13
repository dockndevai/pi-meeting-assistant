import asyncio
import json
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from services.teams import post_teams_summary
from services.ado   import create_work_item
from services.mcp_client import call_mcp_tool

router = APIRouter()

CONFIG_PATH = Path(__file__).parent.parent / "mcp.json"

def get_mcp_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"[approve] Failed to parse mcp.json: {e}")
    return {}


# ── Request models ─────────────────────────────────────────────────────────────
class TeamsSummary(BaseModel):
    title:        str
    summary:      str
    attendees:    list[str] = []
    decisions:    list[str] = []
    next_meeting: Optional[str] = None

class WorkItem(BaseModel):
    type:               str
    title:              str
    description:        str
    priority:           int = 3
    assigned_to:        Optional[str] = None
    tags:               list[str] = []
    area_path:          Optional[str] = None
    iteration_path:     Optional[str] = None
    due_date:           Optional[str] = None   # YYYY-MM-DD

class ApproveRequest(BaseModel):
    summary:    Optional[TeamsSummary] = None   # None = user skipped Teams post
    work_items: list[WorkItem]         = []


# ── POST /api/approve ──────────────────────────────────────────────────────────
@router.post("")
async def approve(req: ApproveRequest):
    results = {"teams": None, "ado": []}
    config = get_mcp_config()
    mcp_servers = config.get("mcpServers", {})

    # ── Fire Teams + all ADO calls concurrently ────────────────────────────────
    # Fuzzy match keys in configured mcpServers
    teams_key = next((k for k in mcp_servers.keys() if "teams" in k.lower()), None)
    ado_key = next((k for k in mcp_servers.keys() if any(x in k.lower() for x in ["devops", "ado", "azure"])), None)

    async def safe_teams():
        if req.summary is None:
            return None
        
        if teams_key:
            server_conf = mcp_servers[teams_key]
            tool_name = server_conf.get("tool_name")
            
            env_conf = server_conf.get("env", {})
            chat_id = env_conf.get("CHAT_ID") or env_conf.get("TEAMS_CHAT_ID") or os.environ.get("TEAMS_CHAT_ID")
            team_id = env_conf.get("TEAM_ID") or env_conf.get("TEAMS_TEAM_ID") or os.environ.get("TEAMS_TEAM_ID")
            channel_id = env_conf.get("CHANNEL_ID") or env_conf.get("TEAMS_CHANNEL_ID") or os.environ.get("TEAMS_CHANNEL_ID")

            if not tool_name:
                if "mcp" in teams_key.lower():
                    if chat_id or (channel_id and "@thread.v2" in channel_id) or (team_id and "@thread.v2" in team_id):
                        tool_name = "send_chat_message"
                    else:
                        tool_name = "send_channel_message"
                else:
                    tool_name = "post_teams_summary"

            try:
                print(f"[approve] Routing Teams publication via MCP server '{teams_key}' (tool: '{tool_name}')...")
                if tool_name == "send_chat_message":
                    target_chat_id = chat_id or channel_id or team_id
                    if not target_chat_id:
                        raise ValueError(
                            f"For tool 'send_chat_message' on '{teams_key}', you must specify CHAT_ID. "
                            "Add it to the 'env' object of 'teams-mcp' in your mcp.json or set it in your .env."
                        )
                    
                    summary_data = req.summary
                    lines = [
                        f"# 📋 {summary_data.title}\n",
                        summary_data.summary,
                        ""
                    ]
                    if summary_data.decisions:
                        lines.append("### Decisions")
                        for dec in summary_data.decisions:
                            lines.append(f"- {dec}")
                        lines.append("")
                    
                    if summary_data.attendees:
                        lines.append(f"**Attendees**: {', '.join(summary_data.attendees)}")
                    
                    if summary_data.next_meeting:
                        lines.append(f"**Next Meeting**: {summary_data.next_meeting}")
                    
                    msg_text = "\n".join(lines)
                    arguments = {
                        "chatId": target_chat_id,
                        "message": msg_text,
                        "format": "markdown"
                    }
                elif tool_name == "send_channel_message":
                    if not team_id or not channel_id:
                        raise ValueError(
                            f"For tool 'send_channel_message' on '{teams_key}', you must specify both TEAM_ID and CHANNEL_ID. "
                            "Add them to the 'env' object of 'teams-mcp' in your mcp.json or set them in your .env."
                        )
                    
                    summary_data = req.summary
                    lines = [
                        f"# 📋 {summary_data.title}\n",
                        summary_data.summary,
                        ""
                    ]
                    if summary_data.decisions:
                        lines.append("### Decisions")
                        for dec in summary_data.decisions:
                            lines.append(f"- {dec}")
                        lines.append("")
                    
                    if summary_data.attendees:
                        lines.append(f"**Attendees**: {', '.join(summary_data.attendees)}")
                    
                    if summary_data.next_meeting:
                        lines.append(f"**Next Meeting**: {summary_data.next_meeting}")
                    
                    msg_text = "\n".join(lines)
                    arguments = {
                        "teamId": team_id,
                        "channelId": channel_id,
                        "message": msg_text,
                        "format": "markdown"
                    }
                else:
                    arguments = req.summary.model_dump()

                res = await call_mcp_tool(teams_key, tool_name, arguments)
                return res
            except Exception as e:
                print(f"[approve] Teams MCP error via '{teams_key}': {e}")
                return {"ok": False, "error": str(e)}
        else:
            try:
                print(f"[approve] Routing Teams publication via direct Webhook...")
                return await post_teams_summary(req.summary.model_dump())
            except Exception as e:
                print(f"[approve] Teams webhook error: {e}")
                return {"ok": False, "error": str(e)}

    async def safe_ado(item: WorkItem):
        if ado_key:
            server_conf = mcp_servers[ado_key]
            tool_name = server_conf.get("tool_name")
            if not tool_name:
                args_str = " ".join(server_conf.get("args", []))
                if "@azure-devops/mcp" in args_str:
                    tool_name = "wit_create_work_item"
                else:
                    tool_name = "create_ado_work_item"

            # Prepare the arguments based on the target tool schema
            arguments = item.model_dump()
            if tool_name == "wit_create_work_item":
                # Find project name from environment variable or fallback to mcp.json args
                project_name = os.environ.get("ADO_PROJECT", "")
                if not project_name:
                    args_list = server_conf.get("args", [])
                    if len(args_list) > 2:
                        project_name = args_list[2]

                fields_list = [
                    {"name": "System.Title", "value": item.title}
                ]
                if item.description:
                    fields_list.append({"name": "System.Description", "value": item.description, "format": "Html"})
                if item.priority:
                    fields_list.append({"name": "Microsoft.VSTS.Common.Priority", "value": str(item.priority)})
                if item.assigned_to:
                    fields_list.append({"name": "System.AssignedTo", "value": item.assigned_to})
                if item.tags:
                    fields_list.append({"name": "System.Tags", "value": "; ".join(item.tags)})
                if item.area_path:
                    fields_list.append({"name": "System.AreaPath", "value": item.area_path})
                elif project_name:
                    fields_list.append({"name": "System.AreaPath", "value": project_name})
                if item.iteration_path:
                    fields_list.append({"name": "System.IterationPath", "value": item.iteration_path})
                elif project_name:
                    fields_list.append({"name": "System.IterationPath", "value": project_name})
                if item.due_date:
                    # ADO accepts ISO date; midnight UTC is fine for date-only fields
                    fields_list.append({"name": "Microsoft.VSTS.Scheduling.DueDate", "value": f"{item.due_date}T00:00:00Z"})

                arguments = {
                    "project": project_name,
                    "workItemType": item.type,
                    "fields": fields_list
                }

            try:
                print(f"[approve] Routing ADO creation for '{item.title}' via MCP server '{ado_key}' (tool: '{tool_name}')...")
                res = await call_mcp_tool(ado_key, tool_name, arguments)
                
                if res.get("ok"):
                    output = res.get("output", "")
                    # Try parsing json if returned as string
                    try:
                        parsed = json.loads(output)
                        item_id = parsed.get("id") or parsed.get("itemId")
                        item_url = parsed.get("url") or parsed.get("itemUrl")
                        return {"title": item.title, "ok": True, "id": item_id or "mcp", "url": item_url}
                    except Exception:
                        pass
                    return {"title": item.title, "ok": True, "output": output}
                return {"title": item.title, "ok": False, "error": res.get("output")}
            except Exception as e:
                print(f"[approve] ADO MCP error for '{item.title}' via '{ado_key}': {e}")
                return {"title": item.title, "ok": False, "error": str(e)}
        else:
            try:
                print(f"[approve] Routing ADO creation for '{item.title}' via direct REST API...")
                return {"title": item.title, **(await create_work_item(item.model_dump()))}
            except Exception as e:
                print(f"[approve] ADO REST error for '{item.title}': {e}")
                return {"title": item.title, "ok": False, "error": str(e)}


    # All tasks launch at the same time
    tasks    = [safe_teams()] + [safe_ado(item) for item in req.work_items]
    outcomes = await asyncio.gather(*tasks)

    results["teams"] = outcomes[0]
    results["ado"]   = list(outcomes[1:])

    all_ok   = (results["teams"] is None or results["teams"].get("ok")) \
               and all(r.get("ok") for r in results["ado"])

    from fastapi.responses import JSONResponse
    return JSONResponse(content=results, status_code=200 if all_ok else 207)
