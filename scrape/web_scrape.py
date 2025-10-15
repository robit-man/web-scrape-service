#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Chrome automation microservice for Hydra.

Boot behavior mirrors other Hydra services:
- Ensures a private virtualenv and installs dependencies on first run.
- Persists configuration in .env with sane defaults.
- Applies global rate limiting and concurrency guard to Selenium operations.
- Exposes REST endpoints for browser lifecycle, navigation, DOM capture, and screenshots.
- Streams structured events over Server-Sent Events so clients can react in real time.
"""

from __future__ import annotations

import base64
import io
import json
import os
import platform
import random
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from queue import Empty, Queue
from typing import Dict, Optional

# ──────────────────────────────────────────────────────────────
# 0) Embedded venv bootstrap (same pattern as other services)
# ──────────────────────────────────────────────────────────────
VENV_DIR = Path.cwd() / ".venv"


def _in_venv() -> bool:
    base = getattr(sys, "base_prefix", None)
    return base is not None and sys.prefix != base


def _ensure_venv_and_reexec() -> None:
    if sys.version_info < (3, 9):
        print("ERROR: Python 3.9+ required.", file=sys.stderr)
        sys.exit(1)
    if _in_venv():
        return
    python = sys.executable
    if not VENV_DIR.exists():
        print(f"[bootstrap] creating virtualenv at {VENV_DIR}", file=sys.stderr)
        subprocess.check_call([python, "-m", "venv", str(VENV_DIR)])
        pip_bin = VENV_DIR / ("Scripts/pip.exe" if os.name == "nt" else "bin/pip")
        subprocess.check_call([str(pip_bin), "install", "--upgrade", "pip"])
    new_env = os.environ.copy()
    new_env["VIRTUAL_ENV"] = str(VENV_DIR)
    if os.name == "nt":
        python_bin = VENV_DIR / "Scripts" / "python.exe"
    else:
        new_env["PATH"] = f"{VENV_DIR}/bin:{new_env.get('PATH', '')}"
        python_bin = VENV_DIR / "bin" / "python"
    os.execve(str(python_bin), [str(python_bin), *sys.argv], new_env)


_ensure_venv_and_reexec()

# ──────────────────────────────────────────────────────────────
# 1) One-time dependency install + config scaffold
# ──────────────────────────────────────────────────────────────
import subprocess  # noqa: E402  (re-import after re-exec)

SCRIPT_PATH = Path(__file__).resolve()
SCRIPT_DIR = SCRIPT_PATH.parent
SETUP_MARKER = SCRIPT_DIR / ".scrape_setup_complete"
OUT_DIR = SCRIPT_DIR / "frames"


def _pip_install(*pkgs: str) -> None:
    subprocess.check_call([sys.executable, "-m", "pip", "install", *pkgs])


if not SETUP_MARKER.exists():
    _pip_install(
        "--upgrade",
        "pip",
        "Flask",
        "Flask-Cors",
        "python-dotenv",
        "requests",
        "beautifulsoup4",
        "lxml",
        "selenium",
        "webdriver-manager",
        "pillow",
    )
    env_path = SCRIPT_DIR / ".env"
    if not env_path.exists():
        env_path.write_text(
            "SCRAPE_API_KEY={key}\n"
            "SCRAPE_BIND=0.0.0.0\n"
            "SCRAPE_PORT=8130\n"
            "SCRAPE_REQUIRE_AUTH=0\n"
            "SCRAPE_MAX_CONCURRENCY=2\n"
            "SCRAPE_QUEUE_TIMEOUT_S=2.0\n"
            "SCRAPE_RATE_LIMIT_RPS=10\n"
            "SCRAPE_RATE_LIMIT_BURST=20\n"
            "SCRAPE_FILE_TTL_S=900\n"
            "SCRAPE_FRAME_KEEPALIVE_S=45\n"
            "SCRAPE_HEADLESS_DEFAULT=1\n".format(key=uuid.uuid4().hex),
            encoding="utf-8",
        )
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    SETUP_MARKER.write_text("ok", encoding="utf-8")
    os.execv(sys.executable, [sys.executable, *sys.argv])

# ──────────────────────────────────────────────────────────────
# 2) Runtime imports (after env ready)
# ──────────────────────────────────────────────────────────────
from flask import Flask, Response, jsonify, request, send_from_directory, g  # noqa: E402
from flask_cors import CORS  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from PIL import Image  # noqa: E402
from selenium import webdriver  # noqa: E402
from selenium.common.exceptions import TimeoutException, WebDriverException  # noqa: E402
from selenium.webdriver.common.by import By  # noqa: E402
from selenium.webdriver.common.keys import Keys  # noqa: E402
from selenium.webdriver.chrome.options import Options  # noqa: E402
from selenium.webdriver.chrome.service import Service  # noqa: E402
from selenium.webdriver.support import expected_conditions as EC  # noqa: E402
from selenium.webdriver.support.ui import WebDriverWait  # noqa: E402
from webdriver_manager.chrome import ChromeDriverManager  # noqa: E402


def log_message(msg: str, level: str = "INFO") -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level.upper()}] {msg}")


class Tools:
    _driver: Optional[webdriver.Chrome] = None

    @staticmethod
    def _find_system_chromedriver() -> Optional[str]:
        candidates = [
            shutil.which("chromedriver"),
            "/usr/bin/chromedriver",
            "/usr/local/bin/chromedriver",
            "/snap/bin/chromium.chromedriver",
            "/usr/lib/chromium-browser/chromedriver",
            "/opt/homebrew/bin/chromedriver",
        ]
        for path in filter(None, candidates):
            if os.path.isfile(path) and os.access(path, os.X_OK):
                try:
                    subprocess.run([path, "--version"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return path
                except Exception:
                    continue
        return None

    @staticmethod
    def open_browser(headless: bool = False, force_new: bool = False) -> str:
        if force_new and Tools._driver:
            try:
                Tools._driver.quit()
            except Exception:
                pass
            Tools._driver = None

        if Tools._driver:
            return "Browser already open"

        chrome_bin = (
            os.getenv("CHROME_BIN")
            or shutil.which("google-chrome")
            or shutil.which("chromium-browser")
            or shutil.which("chromium")
            or "/snap/bin/chromium"
            or "/usr/bin/chromium-browser"
            or "/usr/bin/chromium"
        )

        opts = Options()
        if chrome_bin:
            opts.binary_location = chrome_bin
        if headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--window-size=1920,1080")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--remote-allow-origins=*")
        opts.add_argument(f"--remote-debugging-port={random.randint(45000, 65000)}")

        try:
            log_message("[open_browser] Trying Selenium-Manager…", "DEBUG")
            Tools._driver = webdriver.Chrome(options=opts)
            log_message("[open_browser] Launched via Selenium-Manager.", "SUCCESS")
            return "Browser launched (selenium-manager)"
        except WebDriverException as e:
            log_message(f"[open_browser] Selenium-Manager failed: {e}", "WARNING")

        snap_drv = "/snap/chromium/current/usr/lib/chromium-browser/chromedriver"
        if os.path.exists(snap_drv):
            try:
                log_message(f"[open_browser] Using snap chromedriver at {snap_drv}", "DEBUG")
                Tools._driver = webdriver.Chrome(service=Service(snap_drv), options=opts)
                log_message("[open_browser] Launched via snap chromedriver.", "SUCCESS")
                return "Browser launched (snap chromedriver)"
            except WebDriverException as e:
                log_message(f"[open_browser] Snap chromedriver failed: {e}", "WARNING")

        sys_drv = Tools._find_system_chromedriver()
        if sys_drv:
            try:
                log_message(f"[open_browser] Trying system chromedriver at {sys_drv}", "DEBUG")
                Tools._driver = webdriver.Chrome(service=Service(sys_drv), options=opts)
                log_message("[open_browser] Launched via system chromedriver.", "SUCCESS")
                return "Browser launched (system chromedriver)"
            except WebDriverException as e:
                log_message(f"[open_browser] System chromedriver failed: {e}", "WARNING")

        arch = (platform.machine() or "").lower()
        if arch in ("aarch64", "arm64", "armv8l", "armv7l") and chrome_bin:
            try:
                raw = subprocess.check_output([chrome_bin, "--version"]).decode().strip()
                ver = raw.split()[1]
                url = (
                    f"https://edgedl.me.gvt1.com/edgedl/chrome/chrome-for-testing/"
                    f"{ver}/linux-arm64/chromedriver-linux-arm64.zip"
                )
                tmp_zip = "/tmp/chromedriver_arm64.zip"
                log_message(f"[open_browser] Downloading ARM64 driver from {url}", "DEBUG")
                subprocess.check_call(["wget", "-qO", tmp_zip, url])
                subprocess.check_call(["unzip", "-o", tmp_zip, "-d", "/tmp"])
                subprocess.check_call(["sudo", "mv", "/tmp/chromedriver", "/usr/local/bin/chromedriver"])
                subprocess.check_call(["sudo", "chmod", "+x", "/usr/local/bin/chromedriver"])
                drv = shutil.which("chromedriver")
                log_message(f"[open_browser] Installed ARM64 driver at {drv}", "DEBUG")
                Tools._driver = webdriver.Chrome(service=Service(drv), options=opts)
                log_message("[open_browser] Launched via downloaded ARM64 chromedriver.", "SUCCESS")
                return "Browser launched (downloaded ARM64 chromedriver)"
            except Exception as e:
                log_message(f"[open_browser] ARM64 download/install failed: {e}", "WARNING")

        if arch in ("x86_64", "amd64") and chrome_bin:
            try:
                raw = subprocess.check_output([chrome_bin, "--version"]).decode().strip()
                browser_major = raw.split()[1].split(".")[0]
            except Exception:
                browser_major = "latest"
            try:
                log_message(f"[open_browser] Installing ChromeDriver {browser_major} via webdriver-manager", "DEBUG")
                drv_path = ChromeDriverManager(driver_version=browser_major).install()
                Tools._driver = webdriver.Chrome(service=Service(drv_path), options=opts)
                log_message("[open_browser] Launched via webdriver-manager.", "SUCCESS")
                return "Browser launched (webdriver-manager)"
            except Exception as e:
                log_message(f"[open_browser] webdriver-manager failed: {e}", "ERROR")

        try:
            log_message("[open_browser] Attempting `sudo snap install chromium`…", "DEBUG")
            subprocess.check_call(["sudo", "snap", "install", "chromium"])
            Tools._driver = webdriver.Chrome(service=Service(snap_drv), options=opts)
            log_message("[open_browser] Launched via newly-installed snap chromium.", "SUCCESS")
            return "Browser launched (snap install fallback)"
        except Exception as e:
            log_message(f"[open_browser] Auto-snap install failed or Chrome still not found: {e}", "ERROR")

        raise RuntimeError(
            "No usable Chrome/Chromium driver. Install Chrome and a matching chromedriver, "
            "or set CHROME_BIN and ensure chromedriver is on PATH."
        )

    @staticmethod
    def close_browser() -> str:
        if Tools._driver:
            try:
                Tools._driver.quit()
                log_message("[close_browser] Browser closed.", "DEBUG")
            except Exception:
                pass
            Tools._driver = None
            return "Browser closed"
        return "No browser to close"

    @staticmethod
    def is_browser_open() -> bool:
        return Tools._driver is not None

    @staticmethod
    def navigate(url: str) -> str:
        if not Tools._driver:
            return "Error: browser not open"
        log_message(f"[navigate] → {url}", "DEBUG")
        Tools._driver.get(url)
        return f"Navigated to {url}"

    @staticmethod
    def click(selector: str, timeout: int = 8) -> str:
        if not Tools._driver:
            return "Error: browser not open"
        try:
            drv = Tools._driver
            el = WebDriverWait(drv, timeout).until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
            drv.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            el.click()
            focused = drv.execute_script("return document.activeElement === arguments[0];", el)
            log_message(f"[click] {selector} clicked (focused={focused})", "DEBUG")
            return f"Clicked {selector}"
        except Exception as e:
            log_message(f"[click] Error clicking {selector}: {e}", "ERROR")
            return f"Error clicking {selector}: {e}"

    @staticmethod
    def input(selector: str, text: str, timeout: int = 8) -> str:
        if not Tools._driver:
            return "Error: browser not open"
        try:
            drv = Tools._driver
            el = WebDriverWait(drv, timeout).until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
            drv.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            el.clear()
            el.send_keys(text + Keys.RETURN)
            log_message(f"[input] Sent {text!r} to {selector}", "DEBUG")
            return f"Sent {text!r} to {selector}"
        except Exception as e:
            log_message(f"[input] Error typing into {selector}: {e}", "ERROR")
            return f"Error typing into {selector}: {e}"

    @staticmethod
    def get_dom_snapshot(max_chars: int = 200_000) -> str:
        if not Tools._driver:
            return ""
        try:
            dom = Tools._driver.execute_script("return document.documentElement.outerHTML;")
            if dom and len(dom) > max_chars:
                dom = dom[:max_chars]
            return dom or ""
        except Exception as exc:
            log_message(f"[dom] snapshot failed: {exc}", "WARNING")
            return ""

    @staticmethod
    def scroll(amount: int = 600) -> str:
        if not Tools._driver:
            return "Error: browser not open"
        try:
            Tools._driver.execute_script("window.scrollBy(0, arguments[0]);", amount)
            return f"Scrolled by {amount}"
        except Exception as exc:
            log_message(f"[scroll] failed: {exc}", "WARNING")
            return f"Error scrolling: {exc}"

    @staticmethod
    def screenshot(filename: str = "screenshot.png") -> str:
        if not Tools._driver:
            return "Error: browser not open"
        Tools._driver.save_screenshot(filename)
        return filename

# ──────────────────────────────────────────────────────────────
# 3) Environment configuration
# ──────────────────────────────────────────────────────────────
load_dotenv(SCRIPT_DIR / ".env")

API_KEY = (os.getenv("SCRAPE_API_KEY") or "").strip()
BIND = os.getenv("SCRAPE_BIND", "0.0.0.0")
PORT = int(os.getenv("SCRAPE_PORT", "8130"))
AUTH_REQUIRED = os.getenv("SCRAPE_REQUIRE_AUTH", "0") in ("1", "true", "TRUE")
MAX_CONCURRENCY = max(1, int(os.getenv("SCRAPE_MAX_CONCURRENCY", "2")))
QUEUE_TIMEOUT_S = float(os.getenv("SCRAPE_QUEUE_TIMEOUT_S", "2.0"))
RATE_LIMIT_RPS = max(1, int(os.getenv("SCRAPE_RATE_LIMIT_RPS", "10")))
RATE_LIMIT_BURST = max(1, int(os.getenv("SCRAPE_RATE_LIMIT_BURST", "20")))
FILE_TTL_S = max(60, int(os.getenv("SCRAPE_FILE_TTL_S", "900")))
FRAME_KEEPALIVE_S = max(10, int(os.getenv("SCRAPE_FRAME_KEEPALIVE_S", "45")))
HEADLESS_DEFAULT = os.getenv("SCRAPE_HEADLESS_DEFAULT", "1") in ("1", "true", "TRUE", "yes")

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# ──────────────────────────────────────────────────────────────
# 4) Service state
# ──────────────────────────────────────────────────────────────
_GLOBAL_LOCK = threading.Lock()
_SESSIONS: Dict[str, dict] = {}
_SESSION_EVENTS: Dict[str, Queue] = {}
_CONC_SEM = threading.BoundedSemaphore(MAX_CONCURRENCY)
_RATE_BUCKETS: Dict[str, dict] = {}
_RATE_LOCK = threading.Lock()


def _slot(timeout: Optional[float] = None):
    class _Slot:
        def __init__(self, timeout_val: Optional[float]):
            self.timeout = float(QUEUE_TIMEOUT_S if timeout_val is None else timeout_val)
            self.acquired = False

        def __enter__(self):
            self.acquired = _CONC_SEM.acquire(timeout=self.timeout)
            if not self.acquired:
                raise TimeoutError("scrape at capacity")
            return self

        def __exit__(self, exc_type, exc, tb):
            if self.acquired:
                try:
                    _CONC_SEM.release()
                except Exception:
                    pass

    return _Slot(timeout)


def _session_meta(sid: str) -> Optional[dict]:
    with _GLOBAL_LOCK:
        return _SESSIONS.get(sid)


def _ensure_session(sid: str) -> Queue:
    with _GLOBAL_LOCK:
        meta = _SESSIONS.setdefault(
            sid,
            {
                "created": time.time(),
                "last": time.time(),
                "headless": HEADLESS_DEFAULT,
                "frames": {},
            },
        )
        meta["last"] = time.time()
        if sid not in _SESSION_EVENTS:
            _SESSION_EVENTS[sid] = Queue(maxsize=256)
        return _SESSION_EVENTS[sid]


def _touch_session(sid: str) -> None:
    with _GLOBAL_LOCK:
        meta = _SESSIONS.get(sid)
        if meta is not None:
            meta["last"] = time.time()


def _session_ids() -> list[str]:
    with _GLOBAL_LOCK:
        return list(_SESSIONS.keys())


def _clear_sessions() -> None:
    with _GLOBAL_LOCK:
        _SESSIONS.clear()
        _SESSION_EVENTS.clear()


def _queue_event(sid: str, payload: dict) -> None:
    q = _ensure_session(sid)
    try:
        q.put_nowait(payload)
    except Exception:
        try:
            q.get_nowait()
        except Exception:
            pass
        try:
            q.put_nowait(payload)
        except Exception:
            pass


def _result_ok(message: str) -> bool:
    msg = (message or "").strip().lower()
    return not msg.startswith("error")


# ──────────────────────────────────────────────────────────────
# 5) Rate limit & auth helpers
# ──────────────────────────────────────────────────────────────
def _now() -> float:
    return time.time()


@app.before_request
def _apply_rate_limit():
    ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "0.0.0.0"
    now = _now()
    with _RATE_LOCK:
        bucket = _RATE_BUCKETS.get(ip)
        if not bucket:
            bucket = {"tokens": float(RATE_LIMIT_BURST), "ts": now}
            _RATE_BUCKETS[ip] = bucket
        elapsed = max(0.0, now - bucket.get("ts", now))
        bucket["ts"] = now
        tokens = min(float(RATE_LIMIT_BURST), float(bucket.get("tokens", RATE_LIMIT_BURST)) + elapsed * RATE_LIMIT_RPS)
        if tokens < 1.0:
            return jsonify({"ok": False, "error": "rate limit"}), 429, {"Retry-After": "1"}
        bucket["tokens"] = tokens - 1.0
    g.client_ip = ip


def _auth_ok(req) -> bool:
    if not AUTH_REQUIRED:
        return True
    header_key = (req.headers.get("X-API-Key") or "").strip()
    if API_KEY and header_key and header_key == API_KEY:
        return True
    auth = (req.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer ") and API_KEY and auth.split(None, 1)[1].strip() == API_KEY:
        return True
    return False


# ──────────────────────────────────────────────────────────────
# 6) Background cleaners
# ──────────────────────────────────────────────────────────────
_CLEAN_STOP = threading.Event()


def _cleanup_old_frames() -> None:
    while not _CLEAN_STOP.is_set():
        now = time.time()
        for path in OUT_DIR.glob("*.png"):
            try:
                age = now - path.stat().st_mtime
            except FileNotFoundError:
                continue
            if age > FILE_TTL_S:
                with contextlib.suppress(Exception):
                    path.unlink()
        for sid in _session_ids():
            meta = _session_meta(sid)
            if not meta:
                continue
            last = meta.get("last", 0)
            if now - last > max(FILE_TTL_S, 2 * FRAME_KEEPALIVE_S):
                with _GLOBAL_LOCK:
                    _SESSIONS.pop(sid, None)
                    _SESSION_EVENTS.pop(sid, None)
        _CLEAN_STOP.wait(30.0)


import atexit  # noqa: E402
import contextlib  # noqa: E402

_clean_thread = threading.Thread(target=_cleanup_old_frames, daemon=True)
_clean_thread.start()


@atexit.register
def _shutdown_cleanup():
    _CLEAN_STOP.set()
    with contextlib.suppress(Exception):
        _clean_thread.join(timeout=2.0)


# ──────────────────────────────────────────────────────────────
# 7) Utility responses
# ──────────────────────────────────────────────────────────────
def _ok(**kwargs):
    data = {"ok": True}
    data.update(kwargs)
    return jsonify(data)


def _error(message: str, status: int = 400):
    return jsonify({"ok": False, "error": str(message)}), status


# ──────────────────────────────────────────────────────────────
# 8) Routes
# ──────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return jsonify({"status": "ok", "browser_open": Tools.is_browser_open(), "sessions": len(_SESSIONS)})


@app.post("/session/start")
def session_start():
    if not _auth_ok(request):
        return _error("unauthorized", 401)
    payload = request.get_json(silent=True) or {}
    headless = bool(payload.get("headless", HEADLESS_DEFAULT))
    with _slot():
        msg = Tools.open_browser(headless=headless, force_new=True)
    if not _result_ok(msg):
        return _error(msg, 500)
    sid = uuid.uuid4().hex
    with _GLOBAL_LOCK:
        _SESSIONS.clear()
        _SESSION_EVENTS.clear()
        _SESSIONS[sid] = {
            "created": time.time(),
            "last": time.time(),
            "headless": headless,
            "frames": {},
        }
    _queue_event(sid, {"type": "status", "msg": "browser_started", "detail": msg, "sid": sid, "ts": int(time.time() * 1000)})
    return _ok(session_id=sid, message=msg, headless=headless)


@app.post("/session/close")
def session_close():
    if not _auth_ok(request):
        return _error("unauthorized", 401)
    with _slot():
        msg = Tools.close_browser()
    _clear_sessions()
    return _ok(message=msg)


@app.post("/navigate")
def navigate():
    if not _auth_ok(request):
        return _error("unauthorized", 401)
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return _error("missing url", 400)
    with _slot():
        msg = Tools.navigate(url)
    _queue_event(data.get("sid") or next(iter(_SESSIONS), ""), {"type": "status", "msg": msg, "ts": int(time.time() * 1000)})
    if not _result_ok(msg):
        return _error(msg, 500)
    return _ok(message=msg)


@app.post("/click")
def click_selector():
    if not _auth_ok(request):
        return _error("unauthorized", 401)
    data = request.get_json(silent=True) or {}
    selector = (data.get("selector") or "").strip()
    if not selector:
        return _error("missing selector", 400)
    with _slot():
        msg = Tools.click(selector)
    if not _result_ok(msg):
        return _error(msg, 500)
    _queue_event(data.get("sid") or next(iter(_SESSIONS), ""), {"type": "status", "msg": msg, "ts": int(time.time() * 1000)})
    return _ok(message=msg)


@app.post("/type")
def type_text():
    if not _auth_ok(request):
        return _error("unauthorized", 401)
    data = request.get_json(silent=True) or {}
    selector = (data.get("selector") or "").strip()
    text = data.get("text")
    if not selector:
        return _error("missing selector", 400)
    if text is None:
        return _error("missing text", 400)
    with _slot():
        msg = Tools.input(selector, str(text))
    if not _result_ok(msg):
        return _error(msg, 500)
    _queue_event(data.get("sid") or next(iter(_SESSIONS), ""), {"type": "status", "msg": msg, "ts": int(time.time() * 1000)})
    return _ok(message=msg)


@app.post("/scroll")
def scroll():
    if not _auth_ok(request):
        return _error("unauthorized", 401)
    data = request.get_json(silent=True) or {}
    amount = int(data.get("amount", 600))
    with _slot():
        msg = Tools.scroll(amount)
    if not _result_ok(msg):
        return _error(msg, 500)
    _queue_event(data.get("sid") or next(iter(_SESSIONS), ""), {"type": "status", "msg": msg, "ts": int(time.time() * 1000)})
    return _ok(message=msg)


@app.post("/click_xy")
def click_xy():
    if not _auth_ok(request):
        return _error("unauthorized", 401)
    data = request.get_json(silent=True) or {}
    try:
        x = float(data.get("x"))
        y = float(data.get("y"))
        viewport_w = float(data.get("viewportW") or data.get("viewport_width"))
        viewport_h = float(data.get("viewportH") or data.get("viewport_height"))
        natural_w = float(data.get("naturalW") or data.get("naturalWidth") or viewport_w)
        natural_h = float(data.get("naturalH") or data.get("naturalHeight") or viewport_h)
    except Exception:
        return _error("invalid coordinates", 400)
    if viewport_w <= 0 or viewport_h <= 0:
        return _error("invalid viewport dimensions", 400)
    scale_x = natural_w / max(1.0, viewport_w)
    scale_y = natural_h / max(1.0, viewport_h)
    vx = x * scale_x
    vy = y * scale_y
    with _slot():
        try:
            drv = Tools._driver  # type: ignore[attr-defined]
        except AttributeError:
            drv = None
        if not drv:
            return _error("browser not open", 409)
        result = drv.execute_script(
            """
            const x = arguments[0];
            const y = arguments[1];
            const el = document.elementFromPoint(x, y);
            if (!el) return { ok: false, reason: 'element_from_point_null' };
            try { el.scrollIntoView({block:'center', inline:'center'}); } catch (err) {}
            const rect = el.getBoundingClientRect();
            try {
                el.click();
                return { ok: true, tag: el.tagName, rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height } };
            } catch (err) {
                return { ok: false, reason: err && err.message ? err.message : String(err) };
            }
            """,
            float(vx),
            float(vy),
        )
    if not result or not result.get("ok"):
        return _error(result.get("reason") if isinstance(result, dict) else "click failed", 500)
    _queue_event(
        data.get("sid") or next(iter(_SESSIONS), ""),
        {
            "type": "status",
            "msg": "click_xy",
            "detail": result,
            "ts": int(time.time() * 1000),
        },
    )
    return _ok(message="click_xy", detail=result)


@app.get("/dom")
def dom_snapshot():
    if not _auth_ok(request):
        return _error("unauthorized", 401)
    html = Tools.get_dom_snapshot(max_chars=200_000)
    if not html:
        return _error("no dom (browser closed?)", 409)
    sid = request.args.get("sid") or next(iter(_SESSIONS), "")
    _queue_event(sid, {"type": "dom", "chars": len(html), "ts": int(time.time() * 1000)})
    return _ok(dom=html, length=len(html))


def _record_frame_meta(sid: str, fname: str, width: int, height: int) -> None:
    with _GLOBAL_LOCK:
        meta = _SESSIONS.get(sid)
        if not meta:
            return
        frames = meta.setdefault("frames", {})
        frames[fname] = {"ts": int(time.time() * 1000), "width": width, "height": height}


@app.get("/screenshot")
def screenshot():
    if not _auth_ok(request):
        return _error("unauthorized", 401)
    sid = request.args.get("sid") or next(iter(_SESSIONS), "")
    fname = f"{uuid.uuid4().hex}.png"
    fpath = OUT_DIR / fname
    with _slot():
        msg = Tools.screenshot(str(fpath))
    if not _result_ok(msg):
        return _error(msg, 500)
    try:
        with Image.open(fpath) as im:
            width, height = im.size
    except Exception:
        width = height = 0
    rel_path = f"/frames/{fname}"
    _record_frame_meta(sid, fname, width, height)
    _queue_event(
        sid,
        {
            "type": "frame",
            "file": rel_path,
            "width": width,
            "height": height,
            "ts": int(time.time() * 1000),
        },
    )
    return _ok(file=rel_path, width=width, height=height)


@app.get("/frames/<path:filename>")
def frames(filename):
    if not _auth_ok(request):
        return _error("unauthorized", 401)
    return send_from_directory(OUT_DIR, filename, as_attachment=False, cache_timeout=0)


def _sse_iter(sid: str):
    q = _ensure_session(sid)
    keepalive_deadline = time.time() + FRAME_KEEPALIVE_S
    try:
        while True:
            try:
                payload = q.get(timeout=5.0)
                keepalive_deadline = time.time() + FRAME_KEEPALIVE_S
                data = json.dumps(payload, separators=(",", ":"))
                yield f"data: {data}\n\n"
            except Empty:
                now = time.time()
                if now >= keepalive_deadline:
                    keepalive_deadline = now + FRAME_KEEPALIVE_S
                    yield ":\n\n"
            except GeneratorExit:
                break
    finally:
        _touch_session(sid)


@app.get("/events")
def events():
    if not _auth_ok(request):
        return _error("unauthorized", 401)
    sid = request.args.get("sid") or ""
    if not sid:
        return _error("missing sid", 400)
    _touch_session(sid)
    return Response(_sse_iter(sid), mimetype="text/event-stream")


@app.errorhandler(TimeoutError)
def _timeout_handler(exc):
    return _error(str(exc), 503)


@app.errorhandler(Exception)
def _unhandled(exc):
    print(f"[error] {exc}", file=sys.stderr)
    return _error("internal error", 500)


@app.after_request
def _default_headers(resp):
    resp.headers.setdefault("Cache-Control", "no-store, max-age=0")
    resp.headers.setdefault("Access-Control-Allow-Headers", "Content-Type, Authorization, X-API-Key")
    return resp


if __name__ == "__main__":
    print(f"[service] starting web_scrape on {BIND}:{PORT}", file=sys.stderr)
    app.run(host=BIND, port=PORT, debug=False, threaded=True)
