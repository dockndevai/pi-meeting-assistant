#!/usr/bin/env python3
"""
scripts/transcribe_indic.py

Transcribes a WAV file using the Sarvam batch Speech-to-Text API and writes the
result to <wav_path>.txt so transcribe_and_post.sh needs no other changes.

Required env:
  SARVAM_API_KEY

Optional env:
  SARVAM_BASE_URL=https://api.sarvam.ai
  SARVAM_STT_MODEL=saaras:v3
  SARVAM_STT_MODE=codemix
  SARVAM_LANGUAGE_CODE=unknown
  SARVAM_POLL_INTERVAL_SECONDS=5
  SARVAM_BATCH_TIMEOUT_SECONDS=7200
"""
import os
import sys
import time
from pathlib import Path

import httpx

BASE_URL = os.environ.get("SARVAM_BASE_URL", "https://api.sarvam.ai").rstrip("/")
MODEL_ID = os.environ.get("SARVAM_STT_MODEL", "saaras:v3")
MODE = os.environ.get("SARVAM_STT_MODE", "codemix")
LANG_CODE = os.environ.get("SARVAM_LANGUAGE_CODE", "unknown")
POLL_INTERVAL_SECONDS = int(os.environ.get("SARVAM_POLL_INTERVAL_SECONDS", "5"))
BATCH_TIMEOUT_SECONDS = int(os.environ.get("SARVAM_BATCH_TIMEOUT_SECONDS", "7200"))


def _raise_for_status(response: httpx.Response, context: str) -> None:
    if response.is_success:
        return
    raise RuntimeError(f"{context} {response.status_code}: {response.text}")


def main():
    if len(sys.argv) != 2:
        print("Usage: transcribe_indic.py <path_to_wav>", file=sys.stderr)
        sys.exit(1)

    api_key = os.environ.get("SARVAM_API_KEY", "").strip()
    if not api_key:
        print("Error: SARVAM_API_KEY is not set", file=sys.stderr)
        sys.exit(1)

    wav_path = Path(sys.argv[1])
    if not wav_path.exists():
        print(f"Error: WAV file not found at {wav_path}", file=sys.stderr)
        sys.exit(1)

    headers = {
        "api-subscription-key": api_key,
    }
    print(f"[transcribe_indic] Uploading {wav_path.name} to Sarvam batch STT ({MODEL_ID}, {MODE})...", file=sys.stderr)

    with httpx.Client(timeout=httpx.Timeout(connect=30.0, read=300.0, write=1800.0, pool=30.0)) as client:
        init_response = client.post(
            f"{BASE_URL}/speech-to-text/job/v1",
            headers={**headers, "Content-Type": "application/json"},
            json={
                "job_parameters": {
                    "model": MODEL_ID,
                    "mode": MODE,
                    "language_code": LANG_CODE,
                }
            },
        )
        _raise_for_status(init_response, "Sarvam batch init")
        job_id = init_response.json()["job_id"]

        upload_links_response = client.post(
            f"{BASE_URL}/speech-to-text/job/v1/upload-files",
            headers={**headers, "Content-Type": "application/json"},
            json={"job_id": job_id, "files": [wav_path.name]},
        )
        _raise_for_status(upload_links_response, "Sarvam batch upload-links")
        upload_url = upload_links_response.json()["upload_urls"][wav_path.name]["file_url"]

        with wav_path.open("rb") as audio_file:
            upload_response = client.put(
                upload_url,
                content=audio_file.read(),
                headers={
                    "x-ms-blob-type": "BlockBlob",
                    "Content-Type": "audio/wav",
                },
            )
        _raise_for_status(upload_response, "Sarvam batch blob-upload")

        start_response = client.post(
            f"{BASE_URL}/speech-to-text/job/v1/{job_id}/start",
            headers={**headers, "Content-Type": "application/json"},
            json={},
        )
        _raise_for_status(start_response, "Sarvam batch start")

        deadline = time.time() + BATCH_TIMEOUT_SECONDS
        status_payload = None
        while time.time() < deadline:
            status_response = client.get(
                f"{BASE_URL}/speech-to-text/job/v1/{job_id}/status",
                headers=headers,
            )
            _raise_for_status(status_response, "Sarvam batch status")
            status_payload = status_response.json()
            state = status_payload.get("job_state")
            if state == "Completed":
                break
            if state == "Failed":
                raise RuntimeError(f"Sarvam batch failed: {status_payload.get('error_message') or status_payload}")
            time.sleep(POLL_INTERVAL_SECONDS)
        else:
            raise RuntimeError(f"Sarvam batch timed out after {BATCH_TIMEOUT_SECONDS}s waiting for completion")

        output_files = []
        for detail in status_payload.get("job_details", []):
            for output in detail.get("outputs", []) or []:
                if output.get("file_name"):
                    output_files.append(output["file_name"])

        if not output_files:
            raise RuntimeError(f"Sarvam batch completed without output files: {status_payload}")

        download_links_response = client.post(
            f"{BASE_URL}/speech-to-text/job/v1/download-files",
            headers={**headers, "Content-Type": "application/json"},
            json={"job_id": job_id, "files": output_files},
        )
        _raise_for_status(download_links_response, "Sarvam batch download-links")
        download_url = download_links_response.json()["download_urls"][output_files[0]]["file_url"]

        transcript_response = client.get(download_url)
        _raise_for_status(transcript_response, "Sarvam batch transcript-download")
        payload = transcript_response.json()

    text = (payload.get("transcript") or "").strip()
    out_path = Path(f"{wav_path}.txt")
    out_path.write_text(text, encoding="utf-8")
    print(f"[transcribe_indic] Wrote {out_path} ({len(text)} chars)", file=sys.stderr)


if __name__ == "__main__":
    main()
