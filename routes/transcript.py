"""
routes/transcript.py

Decision: asyncio.Lock over threading.Lock
  FastAPI + uvicorn runs in a single thread using an async event loop.
  asyncio.Lock is the correct primitive here — threading.Lock would work
  but is heavier and designed for multi-threaded code.

Decision: store session in module-level dict, not a database
  This is a single-user tool. One meeting is processed at a time.
  SQLite would add latency on every read/write and complexity for zero
  benefit. The session is cleared after approval — memory stays flat.

Decision: background task for LLM call (respond 202 immediately)
  The Anthropic API call takes 3–8s. If we awaited it in the POST handler,
  the whisper script's curl call would hang for 8 seconds before getting
  a response. Instead: acknowledge immediately (202), run LLM in the
  background, let the dashboard poll for completion. This also means a
  network hiccup between whisper and the server doesn't lose the transcript.
"""

import asyncio
import os
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile
from pydantic import BaseModel

from services.llm import run_llm, run_chat
from services.live import hub

router = APIRouter()

class ChatRequest(BaseModel):
    message: str
    context: Optional[str] = None

MAX_UPLOAD_BYTES = 1024 * 1024 * 1024  # 1GB
CMD_FILE = Path("/tmp/record_cmd")

# ── Session state ─────────────────────────────────────────────────────────────
_session_lock: asyncio.Lock = asyncio.Lock()
_session: Optional[dict]    = None


# ── POST /api/record/start ───────────────────────────────────────────────────
@router.post("/record/start")
async def start_recording():
    global _session
    async with _session_lock:
        if _session and _session.get("status") in ("recording", "processing"):
            raise HTTPException(status_code=400, detail=f"Already in status: {_session.get('status')}")
        _session = {"status": "recording"}

    await hub.start_meeting(source="dashboard")

    # Write start command for the worker
    try:
        CMD_FILE.write_text("start")
    except Exception as e:
        print(f"[record] Failed to write start command: {e}")
        
    return {"status": "recording", "message": "Recording started"}


# ── POST /api/record/active ──────────────────────────────────────────────────
@router.post("/record/active")
async def mark_recording_active():
    """Worker calls this when wake word starts recording (no UI Start click)."""
    global _session
    async with _session_lock:
        if _session and _session.get("status") in ("processing", "pending_approval"):
            return {"status": _session["status"], "message": "left existing session unchanged"}
        _session = {"status": "recording"}
    meeting = await hub.start_meeting(source="wake-word")
    return {"status": "recording", "message": "Recording active", "meeting_id": meeting["id"]}


# ── POST /api/record/processing ──────────────────────────────────────────────
@router.post("/record/processing")
async def mark_recording_processing():
    """
    Worker calls this when recording ends (silence or stop).

    If the live transcript captured the meeting, the LLM runs on it right away
    and the response tells the worker to skip batch STT (live_transcript=true).
    Otherwise the worker falls back to batch-transcribing the saved WAV.
    """
    global _session
    async with _session_lock:
        _session = {"status": "processing"}

    ended = await hub.end_meeting()
    if ended and len(ended["transcript"].strip()) >= 20:
        req = TranscriptRequest(
            transcript=ended["transcript"],
            source_file=f"live-meeting-{ended['meeting']['id']}",
            duration_seconds=ended["duration_seconds"],
        )
        asyncio.create_task(_process_transcript(req))
        return {"status": "processing", "live_transcript": True,
                "message": "Recording ended, summarising live transcript"}

    return {"status": "processing", "live_transcript": False,
            "message": "Recording ended, no live transcript — use batch STT"}


# ── POST /api/record/stop ────────────────────────────────────────────────────
@router.post("/record/stop", status_code=202)
async def stop_recording(background_tasks: BackgroundTasks):
    global _session
    async with _session_lock:
        # Always signal the worker — wake-word sessions may not have set UI state yet.
        was_recording = bool(_session and _session.get("status") == "recording")
        if was_recording or not _session or _session.get("status") == "idle":
            _session = {"status": "processing"}

    # Write stop command for the worker (even if session was out of sync)
    try:
        CMD_FILE.write_text("stop")
    except Exception as e:
        print(f"[record] Failed to write stop command: {e}")

    # If in MOCK mode with no real worker, the meeting never gets a
    # /record/processing call — end it here and use whatever it captured.
    if os.environ.get("MOCK", "true").lower() == "true":
        ended = await hub.end_meeting()
        if ended and len(ended["transcript"].strip()) >= 20:
            background_tasks.add_task(_process_transcript, TranscriptRequest(
                transcript=ended["transcript"],
                source_file=f"live-meeting-{ended['meeting']['id']}",
                duration_seconds=ended["duration_seconds"],
            ))
            return {"status": "processing", "message": "Recording stopped, summarising live transcript"}
        mock_req = TranscriptRequest(
            transcript=(
                "Sprint planning meeting. Attendees: Asha, Vikram, Meera. "
                "Vikram needs to fix the login page bug by Friday. "
                "Asha will set up the staging environment by Wednesday."
            ),
            source_file="mock-dashboard-record",
            duration_seconds=12
        )
        background_tasks.add_task(_process_transcript, mock_req)

    return {"status": "processing", "message": "Recording stopped, processing transcript"}


# ── Request model ─────────────────────────────────────────────────────────────
class TranscriptRequest(BaseModel):
    transcript:       str
    source_file:      str  = "unknown"
    duration_seconds: int  = 0


# ── Background LLM task ───────────────────────────────────────────────────────
async def _process_transcript(req: TranscriptRequest):
    global _session
    try:
        result = await run_llm(req.transcript)
        async with _session_lock:
            _session = {
                "status":           "pending_approval",
                "source_file":      req.source_file,
                "duration_seconds": req.duration_seconds,
                "transcript":       req.transcript,
                "summary":          result["summary"],
                "work_items":       result["work_items"],
            }
        print(f"[transcript] LLM done — {len(result['work_items'])} work items extracted")
    except Exception as e:
        async with _session_lock:
            _session = {"status": "error", "error": str(e)}
        print(f"[transcript] LLM error: {e}")


# ── POST /api/transcript ──────────────────────────────────────────────────────
@router.post("", status_code=202)
async def receive_transcript(req: TranscriptRequest, background_tasks: BackgroundTasks):
    global _session
    # Filter out common silence/blank placeholder tokens from whisper
    cleaned = req.transcript.strip()
    for token in ["[BLANK_AUDIO]", "[Silence]", "[music]", "[laughter]", "[BLANK]"]:
        cleaned = cleaned.replace(token, "")
    cleaned = cleaned.strip()

    if len(cleaned) < 20:
        # Reset session to idle so dashboard is not stuck in "processing"
        async with _session_lock:
            if _session and _session.get("status") == "processing":
                _session = None
        raise HTTPException(
            status_code=400, 
            detail=f"Transcript too short or empty (cleaned: '{cleaned}')"
        )

    print(f"[transcript] Received {len(req.transcript)} chars from {req.source_file}")

    async with _session_lock:
        _session = {"status": "processing"}

    # Return immediately — LLM runs in the background
    background_tasks.add_task(_process_transcript, req)
    return {"status": "processing", "message": "Transcript received, LLM running"}


# ── GET /api/transcript/pending ───────────────────────────────────────────────
@router.get("/pending")
async def get_pending():
    """Dashboard polls this every few seconds to check for results."""
    async with _session_lock:
        if (_session is None or _session.get("status") == "idle") and hub.meeting:
            return {"status": "recording", "meeting_id": hub.meeting["id"]}
        if _session is None:
            return {"status": "idle"}
        return _session


# ── DELETE /api/transcript/pending ───────────────────────────────────────────
@router.delete("/pending")
async def clear_pending(end_live: bool = False):
    """Dashboard calls this after user approves or rejects."""
    global _session
    async with _session_lock:
        _session = None
    # The dashboard's Reset also closes a live meeting left open (e.g. the
    # worker never reported back). Opt-in only: transcribe_and_post.sh calls
    # this on batch failure and must not end a newer meeting in progress.
    if end_live and hub.meeting:
        await hub.end_meeting()
    return {"status": "cleared"}


# ── GET /api/transcript/logs ──────────────────────────────────────────────────
@router.get("/logs")
async def get_logs(lines: int = 100):
    """Returns the last N lines of supervisor server and worker logs.

    stdout is preferred; stderr is filtered (ALSA/JACK spam) and appended so
    useful lines like wake-word / record status stay visible in the UI.
    """
    log_dir = Path("/var/log/supervisor")
    noise_prefixes = (
        "ALSA lib ",
        "Cannot connect to server socket",
        "Cannot connect to server request channel",
        "jack server is not running",
        "JackShmReadWritePtr",
    )

    def read_last_lines(filepath: Path, num: int) -> list[str]:
        if not filepath.exists():
            return []
        try:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                return f.readlines()[-num:]
        except Exception as e:
            return [f"Error reading {filepath.name}: {e}\n"]

    def useful_stderr(filepath: Path, num: int) -> list[str]:
        kept = []
        for line in read_last_lines(filepath, max(num * 4, 200)):
            if any(line.startswith(p) or p in line for p in noise_prefixes):
                continue
            if not line.strip():
                continue
            kept.append(line)
        return kept[-num:]

    def merge_logs(stdout_name: str, stderr_name: str, num: int) -> str:
        # stdout last so auto-scroll lands on wake-word / record lines
        stdout = read_last_lines(log_dir / stdout_name, num)
        stderr = useful_stderr(log_dir / stderr_name, num)
        combined = stderr + stdout
        if not combined:
            return f"No log output yet in {stdout_name} / {stderr_name}."
        return "".join(combined[-num:])

    return {
        "server": merge_logs("server.log", "server-err.log", lines),
        "worker": merge_logs("worker.log", "worker-err.log", lines),
    }


# ── POST /api/transcript/upload ───────────────────────────────────────────────
async def _run_transcription(file_path: Path):
    global _session
    try:
        script_path = Path("/scripts/transcribe_and_post.sh")
        if not script_path.exists():
            # Fallback to local script directory if not running in Docker
            script_path = Path(__file__).parent.parent / "scripts" / "transcribe_and_post.sh"
            
        print(f"[upload] Starting transcription background process: {script_path} {file_path}")
        
        # Run script using asyncio subprocess
        process = await asyncio.create_subprocess_exec(
            "bash", str(script_path), str(file_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        
        if process.returncode != 0:
            err_msg = stderr.decode(errors='ignore')
            print(f"[upload] Transcription failed with code {process.returncode}: {err_msg}")
            async with _session_lock:
                _session = {"status": "error", "error": f"Transcription failed: {err_msg}"}
        else:
            print(f"[upload] Transcription script completed successfully.")
    except Exception as e:
        print(f"[upload] Error running transcription: {e}")
        async with _session_lock:
            _session = {"status": "error", "error": f"Internal upload error: {str(e)}"}


@router.post("/upload", status_code=202)
async def upload_audio(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
):
    """
    Accept raw multipart file upload (not base64 JSON).
    Large WAVs (~500MB+) fail the old base64 path: the browser POST arrives with
    an empty body, which surfaced as "No audio data received".
    """
    global _session
    async with _session_lock:
        if _session and _session.get("status") in ("recording", "processing"):
            raise HTTPException(status_code=400, detail=f"Already in status: {_session.get('status')}")
        _session = {"status": "processing"}

    filename = file.filename or "recording.wav"
    if not filename.lower().endswith(".wav"):
        async with _session_lock:
            _session = None
        raise HTTPException(status_code=400, detail="Please upload a .wav audio file")

    safe_name = re.sub(r"[^\w.\-]+", "_", filename)
    temp_dir = Path("/recordings")
    if not temp_dir.exists():
        temp_dir = Path(__file__).parent.parent / "scratch"
        temp_dir.mkdir(parents=True, exist_ok=True)
    temp_file = temp_dir / f"uploaded_{safe_name}"

    try:
        total = 0
        with temp_file.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    out.close()
                    temp_file.unlink(missing_ok=True)
                    async with _session_lock:
                        _session = None
                    raise HTTPException(
                        status_code=413,
                        detail=f"File too large for browser upload (limit {MAX_UPLOAD_BYTES // (1024*1024)}MB).",
                    )
                out.write(chunk)

        if total == 0:
            temp_file.unlink(missing_ok=True)
            async with _session_lock:
                _session = None
            raise HTTPException(status_code=400, detail="No audio data received — please try the upload again")

        print(f"[upload] Saved uploaded audio to {temp_file} ({total} bytes)")
        background_tasks.add_task(_run_transcription, temp_file)
        return {"status": "processing", "message": "File uploaded successfully, starting transcription"}
    except HTTPException:
        raise
    except Exception as e:
        temp_file.unlink(missing_ok=True)
        async with _session_lock:
            _session = None
        raise HTTPException(status_code=500, detail=f"Failed to process upload: {str(e)}")
    finally:
        await file.close()


# ── POST /api/transcript/chat ─────────────────────────────────────────────────
@router.post("/chat")
async def chat_with_agent(req: ChatRequest):
    context_data = req.context
    response_text = await run_chat(req.message, context_data)
    return {"response": response_text}


