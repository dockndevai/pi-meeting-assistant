# System Design & Architecture Decisions

This document details the components chosen for the **Raspberry Pi Meeting Assistant**, the technical reasons for their selection, and how they compare to alternative technologies.

---

## 1. Recording Trigger

* **Component Used**: `openWakeWord` (`"hey_jarvis"` model on ONNX Runtime) for hands-free trigger, alongside manual start/stop from the dashboard (written to `/tmp/record_cmd`). Both are handled by `record_worker.py`.
* **Role**: Starts microphone recording either on wake word or on demand from the dashboard; stops on silence or a "stop" command.

### Decision: always-on wake-word detection, back alongside manual control
`record_worker.py` keeps a mic stream open at idle and feeds 80ms chunks to openWakeWord, while still polling `/tmp/record_cmd` for a manual "start" — either one triggers a recording. Model is ~1-3MB (baked into the image at build time, see `Dockerfile`), negligible next to IndicWhisper's ~1.5GB checkpoint, so the always-on listening path doesn't add meaningful overhead even on a Pi.

---

## 2. Transcription Engine

* **Component Used**: Sarvam Speech-to-Text, in three tiers:
  1. **Live (default)**: Sarvam Realtime WebSocket (`saaras:v3-realtime`, `codemix`) in `services/stt_stream.py`. Partial text while people speak, one final line per utterance.
  2. **Live backup**: if realtime fails to connect 3 times in a row, the same meeting switches to the synchronous REST API, sending ~12–25s chunks cut at quiet points.
  3. **Batch**: `transcribe_indic.py` on the saved WAV, used only when a meeting produced no live transcript (and for uploaded files).
* **Role**: Turns meeting audio into text, including Hindi and Hindi-English code-mixed speech, as the meeting happens.

### Decision: the server owns the live STT connection
The recording worker only streams raw 16 kHz PCM to the server over a local WebSocket (`/ws/audio`). The server bridges it to Sarvam. There is one ingestion path, so `scripts/replay_audio.py` (a WAV played in from a laptop) exercises exactly the same code as the Pi mic, and the API key and reconnect logic live in one place. The worker still writes the full WAV, so batch STT stays available as a last resort.

### Prior Approach: whisper.cpp + ggml-small.en.bin
We initially shipped `whisper.cpp` compiled natively for the Pi 5 (Cortex-A76 NEON flags), using the `ggml-small.en.bin` model. This was fast and close to real-time, but **`small.en` is English-only** — it produced garbage output on Hindi meeting audio, which is the actual use case here. Stock multilingual Whisper checkpoints also transcribe Hindi noticeably worse than English.

### Alternatives Considered
| Alternative | Strengths | Weaknesses | Comparison vs. IndicWhisper (transformers) |
| :--- | :--- | :--- | :--- |
| **Cloud APIs** *(OpenAI Whisper, Google Cloud STT)* | No local CPU load, maximum transcription accuracy. | Exposes private meeting audio to cloud servers, requires internet connection, incurs per-minute pricing. | **Worse**: Violates data privacy constraints and increases operational costs. |
| **whisper.cpp + multilingual ggml model** | Keeps the fast, compiled C++ runtime; only a model swap. | Stock Whisper (even multilingual `large`) is meaningfully weaker on Hindi and Hindi-English code-mixing than a model fine-tuned for it. IndicWhisper itself isn't distributed as a GGML file — converting the HF checkpoint to GGML is extra one-time setup work. | **Worse for accuracy**: better performance profile, but the accuracy gap on Hindi is the whole point of this change. |
| **IndicWhisper via whisper.cpp (GGML conversion)** | Keeps the lighter compiled-binary runtime, avoids the PyTorch dependency. | Requires converting the HF checkpoint via whisper.cpp's `convert-h5-to-ggml.py` as a one-time step. | **Neutral**: a legitimate lighter-weight path we didn't take — chose the transformers pipeline for a simpler, more maintainable code path over squeezing out CPU performance. |

### Why Sarvam API is Better for This Use Case
1. **Hindi + Code-Mixing Support**: Sarvam is tuned for Indian languages and mixed Hindi-English speech, which matches the meeting use case.
2. **No Persistent Local STT Model**: The container no longer downloads or caches multi-GB transcription checkpoints locally.
3. **Faster Operational Path**: Offloading ASR removes the CPU-only transcription bottleneck on the Pi and simplifies long-recording processing.

---

## 3. Backend API Web Framework

* **Component Used**: `FastAPI` + `Uvicorn` (Single-worker ASGI Server).
* **Role**: Exposes endpoints for the dashboard front-end, manages recording states, and coordinates LLM/DevOps/Teams tasks.

### Alternatives Considered
| Alternative | Strengths | Weaknesses | Comparison vs. FastAPI |
| :--- | :--- | :--- | :--- |
| **Flask** | Simple, lightweight, standard Python framework. | Synchronous (WSGI). When processing an LLM call (which takes 3–8 seconds), the entire server thread blocks. Dashboard polling requests will hang. | **Worse**: Requires multi-threading hacks to avoid blocking the event loop. |
| **Node.js (Express)** | Extremely fast asynchronous performance. | Requires splitting the backend codebase into two languages (Node.js for server, Python for ML/audio), increasing Docker image size and venv complexity. | **Worse**: Complicates maintenance by adding a second runtime dependency. |

### Why FastAPI is Better
1. **Asynchronous (ASGI)**: Native async/await allows the server to run LLM operations and DevOps syncing as background tasks, keeping the dashboard API responsive.
2. **Pydantic Validation**: Auto-validates data models (e.g., meeting work items, transcripts) before sending them, preventing runtime failures.
3. **Single Virtual Environment**: Allows the API server and the recording script to share the same virtual environment, dependencies, and code assets.

---

## 4. Background Process Manager

* **Component Used**: `Supervisor` (`supervisord`).
* **Role**: Launches and monitors the FastAPI server and the Recording worker concurrently as system daemons.

### Alternatives Considered
| Alternative | Strengths | Weaknesses | Comparison vs. Supervisor |
| :--- | :--- | :--- | :--- |
| **FastAPI Background Tasks** | Native to the API framework. | Not suited for long-running infinite loops (like the recording worker). If the server restarts, the loop is killed. | **Worse**: Fails to manage daemon lifecycle properly. |
| **systemd** | Native Linux service manager. | Difficult to run inside Docker containers without granting dangerous root capabilities. | **Worse**: Breaks container isolation principles. |
| **PM2** | Excellent process management. | Node-centric, requiring NPM install steps inside our clean Python image. | **Worse**: Introduces bloated Node dependencies. |

### Why Supervisor is Better
1. **Container Friendly**: Designed to run as PID 1 inside Docker containers, supervising child processes cleanly.
2. **Auto-Restart & Logging**: Automatically captures `stdout` / `stderr` of both processes to `/var/log/supervisor/` and restarts services if they crash.

---

## 5. LLM Provider & Model

* **Component Used**: `google/gemini-3.1-flash-lite` via OpenRouter.
* **Role**: Summarizes the raw transcript and extracts structured Microsoft Teams updates and Azure DevOps work items.

### Alternatives Considered
| Alternative | Strengths | Weaknesses | Comparison vs. OpenRouter/Gemini |
| :--- | :--- | :--- | :--- |
| **Local LLM** *(Llama 3 / Mistral via Ollama)* | 100% local, offline. | Extremely slow on a Pi 5 CPU/GPU. Token generation takes 1–2 minutes, locking up system resources. | **Worse**: Inefficient hardware utilization for large language modeling. |
| **Anthropic Claude 3.5 Sonnet** | Best-in-class reasoning. | High pricing and slower response times. | **Neutral**: Overkill for basic meeting summarization. |
| **OpenAI GPT-4o-mini** | Very fast and cheap. | Smaller context window than Gemini. | **Neutral/Worse**: Gemini offers a larger context window and cheaper pricing. |

### Why OpenRouter & Gemini 3.1 Flash-Lite is Better
1. **Offloaded Computation**: Offloads heavy model processing to the cloud, allowing the Pi to remain cool and responsive.
2. **Gemini 3.1 Flash-Lite**: Exceptionally fast response times (~2-3s), support for deep nesting JSON schemas, and highly cost-effective pricing (pennies for hundreds of meetings).
3. **OpenRouter Abstraction**: The OpenRouter base URL can be swapped to any API endpoint (like a local Ollama instance or OpenAI) in the `.env` file without changing python code.

---

## 6. State Management

* **Component Used**: SQLite (`services/meeting_store.py`, `/data/pmbot.db`) for live meetings and transcript lines, plus the in-memory review session dictionary with `asyncio.Lock`.
* **Role**: Persists the live meeting as it happens, and tracks the result awaiting review.

### Update: live meetings moved to SQLite
The original in-memory choice (below) assumed one short processing step after the meeting. With live transcription, a meeting is a long-running record watched by several screens: a restart must not lose it, and a screen that joins late needs the full history. SQLite is one file, needs no extra container, and one insert per utterance is negligible on a Pi. A meeting left "live" by a restart resumes if it had activity in the last 10 minutes. The post-meeting review session is still in memory; the reasoning below still applies to it.

### Alternatives Considered
| Alternative | Strengths | Weaknesses | Comparison vs. In-Memory |
| :--- | :--- | :--- | :--- |
| **SQLite / PostgreSQL** | Persistent storage, data is kept across server crashes. | Requires database migrations, ORM dependencies, and disk read/write cycles. | **Worse**: Too complex for an ephemeral dashboard. |
| **Redis** | Fast key-value store. | Requires a separate Redis container running alongside, wasting RAM. | **Worse**: Introduces unnecessary network overhead on a local Pi. |

### Why In-Memory is Better
1. **Single-User Scope**: This assistant processes exactly one meeting at a time. Persistent database history is not required since the final destination of the summary is Microsoft Teams and Azure DevOps.
2. **High Speed**: Updates happen in memory instantly.
3. **Concurrency Control**: Protected with an `asyncio.Lock` to prevent race conditions during concurrent API queries.

---

## 7. Live Updates to the Dashboard

* **Component Used**: WebSocket `/ws/live` (FastAPI native), one connection per screen.
* **Role**: Pushes a full snapshot on connect, then transcript lines, partial text, pause state and STT health as they happen.

### Decision: WebSocket push over polling for the live feed
The dashboard is a shared room screen, possibly also open on laptops. Partial transcripts change several times a second, and polling at that rate is wasteful on a Pi while any slower rate feels laggy. Server-sent events would also work, but the WebSocket leaves room for the client-side interaction planned in later phases. Review-state polling (`/api/transcript/pending`) is still in place and gets triggered immediately by WebSocket events.

---

## 8. Web Dashboard Front-End

* **Component Used**: Vanilla HTML5, CSS3 (Vanilla CSS with Flexbox/Grid), and Modern JavaScript.
* **Role**: Displays the live recording state, displays parsed summaries and action items, permits inline edits, and handles approval commands.

### Alternatives Considered
| Alternative | Strengths | Weaknesses | Comparison vs. Vanilla |
| :--- | :--- | :--- | :--- |
| **React / Next.js / Vue** | Component reuse, large ecosystem. | Requires a NodeJS build system (webpack/vite), compiler tools, and compiles into large client bundles. | **Worse**: Heavy build overhead. |
| **TailwindCSS** | Fast utility classes styling. | Adds a build step or relies on a slow client-side CDN script. | **Worse**: Client-side CDN script is bad for page loading performance on edge networks. |

### Why Vanilla Front-end is Better
1. **Zero Build Step**: Served directly by FastAPI's static file mount. Updates can be edited directly and seen instantly with a browser refresh.
2. **Aesthetic Control**: Pure CSS allowed us to write premium dark-mode panels, glassmorphism blur filters, and micro-animations with 0KB extra bundle size.
3. **Performance**: Loads instantly on the Pi without client-side framework overhead.

---

## Live transcription configuration (.env)

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `SARVAM_API_KEY` | — | Required for live and batch STT |
| `STT_BACKEND` | `realtime` | `realtime` or `chunked` (force the REST backup mode) |
| `SARVAM_REALTIME_MODEL` | `saaras:v3-realtime` | or `saaras:v4-realtime` |
| `SARVAM_REALTIME_LANGUAGE` | `hi-IN` | BCP-47 code; `codemix` mode still handles English inside Hindi |
| `SARVAM_STT_MODE` | `codemix` | Output style (shared with batch STT) |
| `SARVAM_REALTIME_STREAM_TYPE` | `balanced` | `fast` for lower latency, lower accuracy |
| `SARVAM_REALTIME_SILENCE_MS` | `800` | Pause length that ends an utterance |
| `DB_PATH` | `/data/pmbot.db` | SQLite location |
| `LIVE_DEV` | — | `true` enables `/api/live/inject` when `MOCK=false` |
