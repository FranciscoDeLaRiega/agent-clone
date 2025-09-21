# Francisco’s Clone — Multi‑Skill AI Agent

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11%2B-blue" alt="Python">
  <img src="https://img.shields.io/badge/Async-asyncio-green" alt="Async">
  <img src="https://img.shields.io/badge/License-MIT-lightgrey" alt="License">
</p>

> A production‑ready agent that routes tasks to the right skill: **math**, **code**, **web automation**, **image understanding**, **hashing pipelines**, and **long‑term memory** — all backed by OpenAI.

---

## Table of Contents
- [Features](#features)
- [Project Layout](#project-layout)
- [Quickstart](#quickstart)
- [Configuration](#configuration)
- [Routing Rules](#routing-rules)
- [Vision I/O](#vision-io)
- [Browser Automation](#browser-automation)
- [Memory](#memory)
- [Testing](#testing)
- [Common Issues](#common-issues)
- [Development](#development)
- [License](#license)
- [Contributing](#contributing)
- [Roadmap](#roadmap)

---

## ✨ Features
- **Deterministic routing**: Sends each request to the right path (**web** / **math** / **code** / **vision** / **memory** / **default**).
- **Vision support**: Accepts images (URL or bytes) and can return a **single‑label** classification when needed.
- **Web automation**: [`browser-use`](https://github.com/browser-use/browser-use) agent for navigation, scraping, and simple interactions.
- **Code execution**: Uses OpenAI **Responses API** code interpreter tool for quick computations.
- **Hash pipelines**: Chained **SHA‑512** / **MD5** with a compact “final digest only” mode.
- **Long‑term memory**: JSON‑backed store with “**Memory note:** …” capture and number‑pair indexing.
- **A2A Server integration**: Clean task lifecycle, artifacts, and status updates.

---

## 🗂️ Project Layout
```text
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
└─ .gitignore (recommended)
```

---

## 🚀 Quickstart

### 1) Requirements
- Python **3.11+**
- An **OpenAI API key** with access to your chosen models
- (Optional) GitHub CLI if you deploy via GH workflows

### 2) Install
```bash
python -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
# If you don’t have requirements.txt yet, typical deps:
# a2a-server uvicorn starlette loguru browser-use openai python-dotenv
```

### 3) Configure environment
```bash
export OPENAI_API_KEY="sk-***"
# Optional: pick affordable default models
export MODEL_PRIMARY="gpt-5"
export MODEL_BROWSER="gpt-5-mini"
```

### 4) Run
```bash
python -m FranciscoClone
# or explicitly:
uvicorn "FranciscoClone.__main__:create_app" --factory --host 0.0.0.0 --port 9000 --reload
```

Expected log:
```text
INFO A2A agent ready on port 9000
```

---

## ⚙️ Configuration

**Models**
- Primary LLM (Responses API & chat fallback): `RoutingExecutor.model` (env: `MODEL_PRIMARY`)
- Browser LLM: `BrowserUseClient(model=...)` (env: `MODEL_BROWSER`)

**Timeouts**
- Browser agent default: `timeout_seconds=150` (tunable)

**Logging**
- Uses **Loguru**; prefer `{}` formatting: `logger.info("Routing decision: {}", route)`

---

## 🧭 Routing Rules

`router.py` decides the path:
- **Vision** if the message includes an image part (kind `file`/`image`, MIME or filename hints, or raw bytes).
- **Web** if text contains strict URLs (`http(s)` / `www.`) or clear browser verbs (open/click/navigate/etc.).
- **Hash** if `md5` or `sha512` are mentioned.
- **Math** if a plausible expression is detected (`\d+(op)\d+`).
- **Code** if “code / python / algorithm / script / function” appears.
- **Memory** if “remember / memory note / save this”.
- Otherwise **default** (LLM).

---

## 🖼️ Vision I/O

- The executor **collects images** from A2A message parts:
  - Accepts `image/*` MIME, filenames like `.png/.jpg`, or raw `bytes` (base64‑encodes).
  - Includes them in the **Responses API** payload via `{"type":"input_image","image_url":...}` (supports `data:` URLs).
- For simple classification tests, it prepends:
  > “Classify the main object in the image. Reply with a **single lowercase label** only.”
- If the request unexpectedly falls back to `chat.completions` (no image support), it returns a neutral token like `image_missing` to avoid misleading labels.

---

## 🌐 Browser Automation

- Thin wrapper around **browser-use**:
  - Retries transient failures and avoids empty outputs.
  - Normalizes Pydantic/dict results to **non‑empty strings** (JSON if structured).
- If the upper layer expects JSON, wrap plain text as `{"text": "..."}` when parsing fails.

---

## 🧠 Memory

- JSON file at `~/.francisco_agent_memory.json` (auto‑created).
- Stores:
  - `notes`: “Memory note: …” suffixes captured from model output.
  - `pairs`: bi‑directional mappings parsed from text like `24244 ↔ 82148`.
- APIs:
  - `append_note(user_key, note)`
  - `summary(user_key, max_chars=...)`
  - `set_pair/find_pair/find_pair_any`
  - `rebuild_pairs_from_all_notes()`

> You can disable persistence by pointing the store to a non‑writable path in tests (falls back to in‑memory).

---

## 🧪 Testing

- **Vision**: Ensure images are included in the Responses payload via `_iter_image_parts(...)`.
- **Web**: Wrapper retries transient failures and never returns empty strings (prevents Pydantic `json_invalid`).
- **Task lifecycle**: On failure, finalize **once** (avoid both `update_status(final=True)` and `complete()`).
- **Quota errors**: 429 `insufficient_quota` handled; show a friendly message or bubble up via `_fail()`.


---

## 🐛 Common Issues

- **429 `insufficient_quota`**: Add billing or use a project/key with quota; optionally switch to a cheaper model (e.g., `gpt-5-mini`) to test.

---
