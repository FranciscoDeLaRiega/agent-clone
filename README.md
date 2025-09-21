Francisco’s Clone — Multi-Skill AI Agent

A production-ready agent that routes tasks to the right skill: math, code, web automation, image understanding, hashing pipelines, and long-term memory — all backed by OpenAI.

[Badges]
- Python: 3.11+
- Async: asyncio
- License: MIT

Features
- Deterministic routing: Sends each request to the right path (web / math / code / vision / memory / default).
- Vision support: Accepts images (URL or bytes) and can return a single-label classification when needed.
- Web automation: browser-use agent for navigation, scraping, and simple interactions.
- Code execution: Uses OpenAI Responses API code interpreter tool for quick computations.
- Hash pipelines: Chained SHA-512 / MD5 with a compact “final digest only” mode.
- Long-term memory: JSON-backed store with “Memory note:” capture and number-pair indexing.
- A2A Server integration: Clean task lifecycle, artifacts, and status updates.

Project Layout
Francisco-clone/
├─ FranciscoClone/
│  ├─ __main__.py               # App entrypoint (Starlette/uvicorn)
│  ├─ router_executor.py        # Main AgentExecutor with routing logic
│  ├─ router.py                 # Request router (web/math/code/vision/...)
│  ├─ memory_management.py      # JSON-backed MemoryStore
│  ├─ browser.py                # BrowserUseClient wrapper
│  ├─ __init__.py
├─ README.md
├─ agent-project.toml

Quickstart
1) Requirements
   - Python 3.11+
   - An OpenAI API key with access to your chosen models
   - (Optional) GitHub CLI if you deploy via GH workflows

2) Install
   python -m venv .venv
   source .venv/bin/activate
   pip install -U pip
   pip install -r requirements.txt

   If you don’t have a requirements.txt, common deps are:
   a2a-server, uvicorn, starlette, loguru, browser-use, openai (new SDK), python-dotenv.

3) Configure environment
   export OPENAI_API_KEY="sk-***"
   # Optional: pick affordable default models
   export MODEL_PRIMARY="gpt-5"
   export MODEL_BROWSER="gpt-5-mini"

4) Run
   python -m FranciscoClone
   # or explicitly:
   uvicorn "FranciscoClone.__main__:create_app" --factory --host 0.0.0.0 --port 9000 --reload

You should see:
   INFO A2A agent ready on port 9000

Configuration
- Models
  - Primary LLM (Responses API & chat fallback): RoutingExecutor.model (env: MODEL_PRIMARY)
  - Browser LLM: BrowserUseClient(model=...) (env: MODEL_BROWSER)
- Timeouts
  - Browser agent default: timeout_seconds=150 (tunable)
- Logging
  - Uses Loguru; prefer {} formatting: logger.info("Routing decision: {}", route)

Routing Rules (high level)
- Vision if a message includes an image part (kind file/image, MIME or filename hints, or raw bytes).
- Web if text contains strict URLs (http(s) / www.) or clear browser verbs (open/click/navigate/etc.).
- Hash if md5 or sha512 are mentioned.
- Math if a plausible expression is detected (\d+(op)\d+).
- Code if “code / python / algorithm / script / function” appears.
- Memory if “remember / memory note / save this”.
- Otherwise default (LLM).

Vision I/O (important details)
- Collects images from A2A message parts:
  - Accepts image/* MIME, filenames like .png/.jpg, or raw bytes (base64-encodes).
  - Includes them in the Responses API payload via {"type":"input_image", "image_url": ...} (supports data: URLs).
- For simple classification tests, it prepends:
  “Classify the main object in the image. Reply with a single lowercase label only.”
- If the request falls back to chat.completions (no image support), it returns a neutral token like image_missing.

Browser Automation
- Thin wrapper around browser-use:
  - Retries transient failures.
  - Normalizes Pydantic/dict results to non-empty strings.
- If your upper layer expects JSON, wrap plain text as {"text": "..."}.

Memory
- JSON file at ~/.francisco_agent_memory.json (auto-created).
- Stores:
  - notes: “Memory note: …” suffixes captured from model output.
  - pairs: bi-directional mappings parsed from text like 24244 ↔ 82148.
- APIs:
  - append_note(user_key, note)
  - summary(user_key, max_chars=...)
  - set_pair/find_pair/find_pair_any
  - rebuild_pairs_from_all_notes()

Testing
- Vision tests: Ensure images are added to the Responses API payload via _iter_image_parts(...).
- Web tests: Wrapper retries transient failures and never returns empty strings (prevents Pydantic json_invalid).
- Task lifecycle: On failure, finalize once (avoid both update_status(final=True) and complete()).
- Quota errors: 429 insufficient_quota handled; show a friendly message or bubble up via _fail().

Run:
  pytest -q

Common Issues & Fixes
- 429 insufficient_quota: Add billing or use a project/key with quota; optionally switch to a cheaper model (e.g., gpt-5-mini) to test.
- “Task already in a terminal state”: Don’t finalize twice in _fail().
- “I don’t see an image”: Ensure _iter_image_parts() is used in _handle_llm() and router accepts file or image kinds.
- Loguru showing %s: Use {} or f-strings, not printf-style.

Development
- Hot reload:
  uvicorn "FranciscoClone.__main__:create_app" --factory --reload

- Lint/format:
  ruff check .
  black .
  mypy .
