# Hydra Chrome Automation Microservice

A minimal REST microservice that automates a local Chrome/Chromium instance with Selenium. It mirrors other Hydra services:

* Creates and uses a private virtualenv on first run.
* Bootstraps dependencies and writes a `.env` file with sane defaults.
* Applies global **rate limiting** and a **concurrency guard** to Selenium operations.
* Exposes endpoints for browser lifecycle, navigation, DOM capture, scrolling, history, and screenshots.
* Streams structured **Server-Sent Events (SSE)** so clients can react in real time.

---

## Contents

* [Features](#features)
* [Requirements](#requirements)
* [Quick Start](#quick-start)
* [Configuration](#configuration)
* [Runtime Behavior](#runtime-behavior)
* [API Reference](#api-reference)

  * [Health](#get-health)
  * [Session](#post-sessionstart--post-sessionclose)
  * [Navigation & Actions](#post-navigate--post-click--post-type--scroll--history)
  * [Coordinate Click](#post-click_xy)
  * [DOM Snapshot](#get-dom)
  * [Screenshot](#get-screenshot)
  * [Frame Files](#get-framesfilename)
  * [Events (SSE)](#get-events-sse)
* [Authentication](#authentication)
* [Rate Limiting](#rate-limiting)
* [Concurrency & Capacity](#concurrency--capacity)
* [Events Schema](#events-schema)
* [Operational Notes](#operational-notes)
* [Troubleshooting](#troubleshooting)
* [Security Considerations](#security-considerations)
* [License](#license)

---

## Features

* **Self-bootstrapping**: Creates `.venv`, upgrades `pip`, installs Python deps, and writes `.env` on first run.
* **Chrome driver discovery**: Tries Selenium Manager, snap/system `chromedriver`, `webdriver-manager`, and architecture-specific fallbacks (x86_64/ARM64).
* **Single active browser** per process with session metadata and event queues.
* **Token-bucket rate limiting** per client IP.
* **Bounded concurrency** around Selenium operations.
* **SSE event stream** (`/events`) for `status`, `dom`, and `frame` updates.
* **Screenshots** are saved to `./frames/` and available inline (base64) and via static file serving.

---

## Requirements

* **Python** 3.9+
* **Chrome or Chromium** installed

  * Optionally set `CHROME_BIN` to point at the browser binary.
* **Chromedriver** (usually auto-managed)

  * The service attempts: Selenium Manager → snap/system chromedriver → `webdriver-manager` (x86_64) → Chrome-for-Testing ARM64 download → `snap install chromium` fallback.
  * Some fallbacks may require `sudo`.

---

## Quick Start

```bash
cd scrape && python3 web_scrape.py
```

```bash
# On first run, it will:
#  - Create .venv/
#  - Install dependencies
#  - Write .env with defaults
#  - Re-exec under the virtualenv and start the server

# 2) In another terminal, set convenience vars:
cd /path/to/hydra-scrape
export API="http://127.0.0.1:8130"
export KEY="$(grep '^SCRAPE_API_KEY=' .env | cut -d= -f2)"

# 3) Check health:
curl -s "$API/health" | jq

# 4) Start a browser session (headless by default via .env):
curl -s -X POST "$API/session/start" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $KEY" \
  -d '{"headless": true}' | jq
# → Save "session_id" from the response:
export SID="<value from response>"

# 5) Navigate to a URL:
curl -s -X POST "$API/navigate" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $KEY" \
  -d '{"sid":"'"$SID"'", "url":"https://example.com"}' | jq

# 6) Take a screenshot:
curl -s "$API/screenshot?sid=$SID" -H "X-API-Key: $KEY" | jq

# 7) Stream events (SSE):
curl -N "$API/events?sid=$SID" -H "X-API-Key: $KEY"
```

The service listens on `SCRAPE_BIND:SCRAPE_PORT` (defaults: `0.0.0.0:8130`).

---

## Configuration

Configuration is read from `.env` in the script directory (auto-generated on first run). Defaults:

| Variable                   | Default     | Description                                            |
| -------------------------- | ----------- | ------------------------------------------------------ |
| `SCRAPE_API_KEY`           | random UUID | API key used when auth is required.                    |
| `SCRAPE_BIND`              | `0.0.0.0`   | Bind address for the Flask server.                     |
| `SCRAPE_PORT`              | `8130`      | Server port.                                           |
| `SCRAPE_REQUIRE_AUTH`      | `0`         | Set `1`/`true` to require API key.                     |
| `SCRAPE_MAX_CONCURRENCY`   | `2`         | Max concurrent Selenium ops.                           |
| `SCRAPE_QUEUE_TIMEOUT_S`   | `0`         | Max time to wait for a concurrency slot (`0` = block). |
| `SCRAPE_RATE_LIMIT_RPS`    | `10`        | Token refill rate per IP (requests/sec).               |
| `SCRAPE_RATE_LIMIT_BURST`  | `20`        | Max tokens (burst) per IP.                             |
| `SCRAPE_FILE_TTL_S`        | `900`       | TTL for files in `./frames` (seconds).                 |
| `SCRAPE_FRAME_KEEPALIVE_S` | `45`        | SSE keepalive heartbeat interval (seconds).            |
| `SCRAPE_HEADLESS_DEFAULT`  | `1`         | Default headless mode for browser sessions.            |
| `CHROME_BIN`               | *(unset)*   | Optional path to Chrome/Chromium binary.               |

> Note: Code defaults may differ if `.env` values are removed; the scaffold above is what the script writes initially.

---

## Runtime Behavior

* **Virtualenv:** The script re-execs itself under `./.venv` and installs dependencies:

  * `Flask`, `Flask-Cors`, `python-dotenv`, `requests`, `beautifulsoup4`, `lxml`, `selenium`, `webdriver-manager`, `pillow`
* **Frames directory:** Screenshots are written to `./frames/`. A background cleaner removes files older than `SCRAPE_FILE_TTL_S`.
* **Sessions:** A single active browser (Chrome/Chromium) per process. Starting a new session clears previous session metadata and queues.
* **Logging:** Timestamps and levels are printed to stderr/stdout.

---

## API Reference

All success responses use the shape: `{"ok": true, ...}`. Errors use `{"ok": false, "error": "<message>"}` with appropriate HTTP status.

### `GET /health`

Basic service status.

**Response**

```json
{
  "status": "ok",
  "browser_open": false,
  "sessions": 0
}
```

---

### `POST /session/start`  •  `POST /session/close`

Start or close the single browser session.

**Request (start)**

```json
{ "headless": true }
```

**Response (start)**

```json
{
  "ok": true,
  "session_id": "<sid>",
  "message": "Browser launched (...)",
  "headless": true
}
```

**Response (close)**

```json
{ "ok": true, "message": "Browser closed" }
```

> Starting a session emits an SSE `status` event with `msg="browser_started"` and the new `sid`.

---

### `POST /navigate`  •  `POST /click`  •  `POST /type`  •  `POST /scroll`  •  `POST /scroll/up`  •  `POST /scroll/down`  •  `POST /history/back`  •  `POST /history/forward`

High-level browser actions.

**Requests**

* `POST /navigate`

  ```json
  { "sid": "<sid>", "url": "https://example.com" }
  ```

* `POST /click`

  ```json
  { "sid": "<sid>", "selector": "a.primary" }
  ```

* `POST /type`

  ```json
  { "sid": "<sid>", "selector": "input[name=q]", "text": "hydra\n" }
  ```

* `POST /scroll` (down by default)

  ```json
  { "sid": "<sid>", "amount": 600 }
  ```

* `POST /scroll/up`

  ```json
  { "sid": "<sid>", "amount": 600 }
  ```

* `POST /scroll/down`

  ```json
  { "sid": "<sid>", "amount": 600 }
  ```

* `POST /history/back` and `POST /history/forward`

  ```json
  { "sid": "<sid>" }
  ```

**Responses**

```json
{ "ok": true, "message": "..." }
```

Each successful call queues a `status` event on the session SSE stream.

---

### `POST /click_xy`

Click by viewport coordinates (useful when CSS selectors are difficult).

**Request**

```json
{
  "sid": "<sid>",
  "x": 512, "y": 384,                   // viewport space
  "viewportW": 1280, "viewportH": 800,  // current viewport size (required)
  "naturalW": 1280, "naturalH": 800     // page's "natural" width/height (optional)
}
```

**Response**

```json
{
  "ok": true,
  "message": "click_xy",
  "detail": {
    "ok": true,
    "tag": "A",
    "rect": { "x": 100, "y": 200, "width": 120, "height": 20 }
  }
}
```

> The service computes a scale from `viewport*` to `natural*` and clicks the element at the transformed point. On success, a `status` event is emitted.

---

### `GET /dom`

Return a DOM snapshot (outerHTML) truncated to ~200,000 characters.

**Query**

```
/dom?sid=<sid>
```

**Response**

```json
{
  "ok": true,
  "dom": "<!doctype html> ...",
  "length": 123456
}
```

Emits an SSE `dom` event with `chars=<length>`.

---

### `GET /screenshot`

Capture a PNG screenshot and return both an inline base64 and a file path under `/frames`.

**Query**

```
/screenshot?sid=<sid>
```

**Response**

```json
{
  "ok": true,
  "file": "/frames/2a8f...c1.png",
  "width": 1920,
  "height": 1080,
  "mime": "image/png",
  "b64": "<base64 data>"
}
```

Also emits an SSE `frame` event with file path, dimensions, MIME, and base64.

---

### `GET /frames/<filename>`

Serve a previously captured image from `./frames/`.

**Example**

```
GET /frames/2a8f...c1.png
```

---

### `GET /events` (SSE)

Event stream for a session.

**Query**

```
/events?sid=<sid>
```

**Response**

* Content-Type: `text/event-stream`
* Messages are emitted as `data: {...}\n\n` JSON payloads.
* Periodic `":\n\n"` comments are sent as keepalives every `SCRAPE_FRAME_KEEPALIVE_S` seconds.

**Example**

```bash
curl -N -H "X-API-Key: $KEY" "$API/events?sid=$SID"
```

---

## Authentication

If `SCRAPE_REQUIRE_AUTH=1` (or `true`), requests must include either:

* `X-API-Key: <SCRAPE_API_KEY>`
* `Authorization: Bearer <SCRAPE_API_KEY>`

Otherwise, endpoints return `401` with `{"ok": false, "error": "unauthorized"}`.

---

## Rate Limiting

A token-bucket is applied **per client IP**:

* Refill: `SCRAPE_RATE_LIMIT_RPS` tokens/sec
* Burst capacity: `SCRAPE_RATE_LIMIT_BURST`
* On depletion: `429` with `{"ok": false, "error":"rate limit"}` and `Retry-After: 1`.

---

## Concurrency & Capacity

A global `BoundedSemaphore(SCRAPE_MAX_CONCURRENCY)` throttles Selenium operations. Each endpoint acquires a slot:

* If `SCRAPE_QUEUE_TIMEOUT_S > 0`, requests wait up to that many seconds before returning `503` with `{"ok": false, "error":"scrape at capacity"}`.
* If `SCRAPE_QUEUE_TIMEOUT_S == 0` (default in scaffold), requests **block** until a slot is available.

---

## Events Schema

Events are JSON objects emitted on `/events` for a given `sid`.

* **Status**

  ```json
  { "type": "status", "msg": "browser_started", "detail": "Browser launched (...)", "sid": "<sid>", "ts": 1710000000000 }
  ```

  Other `msg` values include navigation and interactions (e.g., `"Clicked <selector>"`, `"Scrolled by <n>"`).

* **DOM**

  ```json
  { "type": "dom", "chars": 123456, "ts": 1710000000000 }
  ```

* **Frame**

  ```json
  {
    "type": "frame",
    "file": "/frames/2a8f...c1.png",
    "width": 1920, "height": 1080,
    "mime": "image/png",
    "b64": "<base64>",
    "ts": 1710000000000
  }
  ```

---

## Operational Notes

* **Single browser**: Only one browser session is managed at a time. Calling `/session/start` clears previous session metadata and queues.
* **Driver selection**: The service tries multiple strategies (Selenium Manager, snap/system, `webdriver-manager`, architecture-specific installers). Some paths use `sudo` and may prompt if not pre-authorized.
* **Headless**: Default comes from `SCRAPE_HEADLESS_DEFAULT`; can be overridden per session with `{"headless": true/false}` in `/session/start`.
* **CORS**: Enabled for all routes.

---

## Troubleshooting

* **`browser not open` / `no dom (browser closed?)` / `409`**
  Start a session first: `POST /session/start`.

* **`rate limit` / `429`**
  Slow down requests or raise limits via `.env`.

* **`scrape at capacity` / `503`**
  Increase `SCRAPE_MAX_CONCURRENCY`, lower request volume, or adjust `SCRAPE_QUEUE_TIMEOUT_S`.

* **Chromedriver errors**
  Ensure Chrome/Chromium is installed and versions match. You may set `CHROME_BIN` and install a matching `chromedriver` on `PATH`. On ARM64/x86_64, the service attempts to self-install; failures here may require manual setup.

---

## Security Considerations

* Treat `SCRAPE_API_KEY` as a secret; rotate it regularly.
* When exposing beyond localhost, run behind a reverse proxy with TLS.
* Headless automation can interact with arbitrary websites; restrict access to trusted clients and consider network egress controls.
* SSE responses may include base64 screenshots; ensure consumers handle sensitive content appropriately.
* Be cautious with endpoints that execute page JavaScript (e.g., `click_xy` logic uses `document.elementFromPoint` and `el.click()`).
