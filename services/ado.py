"""
services/ado.py

Decision: construct ADO PAT auth in Python, not shell
  Base64 encoding ":PAT" is simple in Python. Doing it in shell requires
  either echo + base64 (platform-dependent flags on macOS vs Linux)
  or Python anyway. Keep it in Python.

Decision: sequential work item creation in the route, concurrent here
  The concurrent gather is in approve.py. This function handles one item.
  Keeping services focused on one responsibility makes them testable.
"""

import base64
import os

import httpx

_client = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=5.0),
)


async def create_work_item(payload: dict) -> dict:
    org         = os.environ.get("ADO_ORG", "")
    project     = os.environ.get("ADO_PROJECT", "")
    pat         = os.environ.get("ADO_PAT", "")

    if not all([org, project, pat]):
        raise ValueError("ADO_ORG, ADO_PROJECT, ADO_PAT must all be set")

    item_type   = payload.get("type", "Task")
    title       = payload.get("title", "")
    description = payload.get("description", "")
    assigned_to = payload.get("assigned_to")
    priority    = payload.get("priority", 3)
    tags        = payload.get("tags", [])
    area_path   = payload.get("area_path") or project
    iteration_path = payload.get("iteration_path") or project
    due_date    = payload.get("due_date")

    # Decision: encode project name in the URL (spaces → %20)
    #   ADO project names commonly have spaces ("My Project").
    #   httpx handles params encoding but we build the URL manually here
    #   because the $Type segment must be URL-encoded too.
    from urllib.parse import quote
    url = (
        f"https://dev.azure.com/{org}/{quote(project, safe='')}"
        f"/_apis/wit/workitems/${quote(item_type, safe='')}?api-version=7.1"
    )

    # Decision: Basic auth with empty username
    #   ADO PAT auth requires Base64(":PAT") — colon prefix, empty username.
    auth_header = "Basic " + base64.b64encode(f":{pat}".encode()).decode()

    # ADO uses JSON Patch (RFC 6902) for work item creation
    # Area/Iteration default to the project root when not given.
    patch = [
        {"op": "add", "path": "/fields/System.Title",       "value": title},
        {"op": "add", "path": "/fields/System.Description",
         "value": f"<p>{description.replace(chr(10), '<br>')}</p>"},
        {"op": "add", "path": "/fields/Microsoft.VSTS.Common.Priority", "value": priority},
        {"op": "add", "path": "/fields/System.AreaPath", "value": area_path},
        {"op": "add", "path": "/fields/System.IterationPath", "value": iteration_path},
    ]
    if assigned_to:
        patch.append({"op": "add", "path": "/fields/System.AssignedTo", "value": assigned_to})
    if tags:
        patch.append({"op": "add", "path": "/fields/System.Tags", "value": "; ".join(tags)})
    if due_date:
        patch.append({"op": "add", "path": "/fields/Microsoft.VSTS.Scheduling.DueDate",
                      "value": f"{due_date}T00:00:00Z" if "T" not in str(due_date) else due_date})

    res = await _client.post(
        url,
        json=patch,
        headers={
            "Content-Type":  "application/json-patch+json",
            "Authorization": auth_header,
        },
    )

    if res.status_code not in (200, 201):
        raise RuntimeError(f"ADO API {res.status_code}: {res.text}")

    created = res.json()
    item_id  = created.get("id")
    item_url = created.get("_links", {}).get("html", {}).get("href")
    print(f"[ado] Created {item_type} #{item_id}: {title}")
    return {"ok": True, "id": item_id, "url": item_url}
