"""
main.py — Cerebrium entry point for the ComfyUI serverless deployment.
Converted from anr2me/modal-comfyui (Modal) → Cerebrium.

Architecture (single container, GPU allocated here):
  Cold start  → start ComfyUI subprocess → wait for port 8188 → accept requests
  Each request → proxy HTTP/WebSocket to local ComfyUI on port 8188

What is NOT here (moved to shell_commands in cerebrium.toml, NO GPU used):
  • apt / pip / torch installs
  • comfy-cli + ComfyUI install
  • custom node git clones and their pip deps

Modal concept mapping:
  ComfyMix.start_checkpoint()  → lifespan() startup
  ComfyMix.api()               → this FastAPI app
  shared_dict                  → in-process asyncio state (single container)
  get_remote_url("ComfyGPU")   → always http://127.0.0.1:8188 (local ComfyUI)
  wait_for_port()              → wait_for_comfyui()
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import subprocess
import sys
import time
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx
import websockets
from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocketState
from websockets.connection import State
from websockets.exceptions import ConnectionClosedError

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# Apps communicate using a consistent internal endpoint format: http://api.aws/v4/<project_id>/<app_name>/<func_name> 
APP_NAME = os.getenv("APP_NAME")

COMFYUI_PORT    = 8188
COMFYUI_HOST    = "127.0.0.1"
COMFYUI_URL     = f"http://{COMFYUI_HOST}:{COMFYUI_PORT}"

USER_DIR        = Path("/cache/ComfyUI/user")
OUTPUT_DIR      = Path("/cache/ComfyUI/output")
INPUT_DIR       = Path("/cache/ComfyUI/input")

STARTUP_TIMEOUT = 300   # seconds — Modal equivalent: startup_timeout=300
PROXY_TIMEOUT   = 120   # seconds for general HTTP proxy calls

# ─────────────────────────────────────────────────────────────────────────────
# State (replaces Modal shared_dict — single process, no cross-container state)
# ─────────────────────────────────────────────────────────────────────────────
_comfyui_proc: Optional[subprocess.Popen] = None
_pending_prompt: int = 0           # incremented before forward, decremented after
_inqueue:        int = 0           # last known ComfyUI queue depth (from WS status msgs)
_pending_lock = asyncio.Lock()     # guard _pending_prompt (fixes Modal bug #2)


# ─────────────────────────────────────────────────────────────────────────────
# ComfyUI process management
# ─────────────────────────────────────────────────────────────────────────────

import toml

def get_project_id_from_config(config_path="cerebrium.toml") -> str:
    with open(config_path, "r") as f:
        config = toml.load(f)
    
    # Access the project_name or project_id in your TOML
    return config.get("project_name")


def _ensure_dirs() -> None:
    for d in [USER_DIR / "default/workflows", OUTPUT_DIR, INPUT_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def _start_comfyui() -> None:
    global _comfyui_proc
    _ensure_dirs()

    cmd = (
        f"comfy manager enable-legacy-gui && "
        f"comfy launch --background -- "
        f"--listen 0.0.0.0 "
        f"--port {COMFYUI_PORT} "
        f"--enable-cors-header '*' "
        f"--user-directory {USER_DIR} "
        f"--output-directory {OUTPUT_DIR} "
        f"--input-directory {INPUT_DIR}"
    )
    log.info(f"Starting ComfyUI: {cmd}")
    _comfyui_proc = subprocess.Popen(
        cmd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    # Stream ComfyUI stdout to our logs (daemon thread — won't block shutdown)
    def _stream(proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            log.info("[ComfyUI] " + line.decode(errors="replace").rstrip())

    threading.Thread(target=_stream, args=(_comfyui_proc,), daemon=True).start()


def _wait_for_port(port: int, timeout: int = STARTUP_TIMEOUT) -> None:
    """Block until port accepts connections or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((COMFYUI_HOST, port), timeout=1):
                log.info(f"ComfyUI is ready on port {port}.")
                return
        except OSError:
            time.sleep(0.5)
    raise TimeoutError(f"ComfyUI did not become ready on port {port} within {timeout}s")


# ─────────────────────────────────────────────────────────────────────────────
# Lifespan — cold-start setup
# Modal equivalent: ComfyMix.start_checkpoint() + @modal.enter(snap=True)
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("=== Cold start: launching ComfyUI ===")

    # Download any missing models to /persistent-storage/cache (skips if already cached).
    # Runs on first cold start only; subsequent starts skip immediately.
    # models.py is available here (files are in / at runtime).
    try:
        import download_models
        download_models.run()
    except Exception as exc:
        log.warning(f"Model download step failed (non-fatal): {exc}")

    # Launch ComfyUI
    _start_comfyui()
    _wait_for_port(COMFYUI_PORT, timeout=STARTUP_TIMEOUT)
    log.info("=== ComfyUI ready — accepting requests ===")
    yield
    
    # Shutdown
    if _comfyui_proc and _comfyui_proc.poll() is None:
        log.info("Terminating ComfyUI process …")
        _comfyui_proc.terminate()
        try:
            _comfyui_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _comfyui_proc.kill()
    log.info("=== Shutdown complete ===")


app = FastAPI(title="ComfyUI on Cerebrium", lifespan=lifespan)


# ─────────────────────────────────────────────────────────────────────────────
# Health check (required by Cerebrium)
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    if _comfyui_proc is None or _comfyui_proc.poll() is not None:
        return JSONResponse({"status": "error", "detail": "ComfyUI not running"}, status_code=503)
    return {"status": "ok"}


# ─────────────────────────────────────────────────────────────────────────────
# HTTP proxy helpers
# Modal equivalent: forward_httpx()
# ─────────────────────────────────────────────────────────────────────────────

_STRIP_HEADERS = frozenset([
    "host", "content-length",
    "x-forwarded-proto", "x-forwarded-for",
    "x-forwarded-host", "x-forwarded-port",
])


def _proxy_headers(request: Request) -> dict:
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _STRIP_HEADERS
    }
    # Only accept encodings that httpx will auto-decompress; avoids zstd blob bug
    headers["accept-encoding"] = "gzip, br, deflate"
    return headers


async def _forward(
    request: Request,
    target_url: str,
    *,
    try_json: bool = False,
    timeout: float = PROXY_TIMEOUT,
) -> Response:
    headers = _proxy_headers(request)
    body    = await request.body()

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.request(
            method  = request.method,
            url     = target_url,
            params  = request.query_params,
            headers = headers,
            content = body,
        )

    if try_json:
        try:
            return JSONResponse(resp.json(), status_code=resp.status_code)
        except Exception as exc:
            log.warning(f"JSON decode failed for {request.url.path}: {exc!r}")

    return Response(
        content     = resp.content,
        status_code = resp.status_code,
        headers     = dict(resp.headers),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Prompt / queue endpoints  (trigger GPU work)
# Modal equivalent: proxy_prompt() + pending_prompt counter
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/prompt")
@app.post("/api/prompt")
@app.post("/queue")
@app.post("/api/queue")
async def proxy_prompt(request: Request):
    global _pending_prompt
    async with _pending_lock:
        _pending_prompt += 1
    log.info(f"prompt queued (pending={_pending_prompt})")

    try:
        target = f"{COMFYUI_URL}{request.url.path}"
        return await _forward(request, target, try_json=True)
    finally:
        async with _pending_lock:
            _pending_prompt = max(0, _pending_prompt - 1)
        log.info(f"prompt done (pending={_pending_prompt})")


@app.get("/prompt")
@app.get("/api/prompt")
@app.get("/queue")
@app.get("/api/queue")
async def proxy_queue_get(request: Request):
    target = f"{COMFYUI_URL}{request.url.path}"
    return await _forward(request, target, try_json=True)


# ─────────────────────────────────────────────────────────────────────────────
# Interrupt
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/interrupt")
@app.post("/api/interrupt")
async def proxy_interrupt(request: Request):
    target = f"{COMFYUI_URL}{request.url.path}"
    return await _forward(request, target, try_json=True)


# ─────────────────────────────────────────────────────────────────────────────
# View (binary — images/videos, no JSON decode)
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/view")
@app.get("/view")
async def proxy_view(request: Request):
    target = f"{COMFYUI_URL}{request.url.path}"
    return await _forward(request, target, try_json=False)


# ─────────────────────────────────────────────────────────────────────────────
# Internal logs (PATCH + GET)
# ─────────────────────────────────────────────────────────────────────────────

@app.patch("/internal/logs{path:path}")
@app.get("/internal/logs{path:path}")
async def proxy_logs(request: Request, path: str):
    target = f"{COMFYUI_URL}{request.url.path}"
    return await _forward(request, target, try_json=True)


# ─────────────────────────────────────────────────────────────────────────────
# Other API / internal routes
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/{path:path}")
@app.get("/internal/{path:path}")
async def proxy_api(request: Request, path: str):
    target = f"{COMFYUI_URL}{request.url.path}"
    return await _forward(request, target, try_json=True)


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket proxy
# Modal equivalent: proxy_websocket() — simplified for single-container setup.
# No CPU↔GPU switching needed; ComfyUI is always local.
# ─────────────────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def proxy_websocket(client_ws: WebSocket):
    await client_ws.accept()

    # Strip reverse-proxy headers before forwarding to local ComfyUI
    headers = {
        k: v for k, v in client_ws.headers.items()
        if k.lower() not in _STRIP_HEADERS
    }
    headers["accept-encoding"] = "gzip, br, deflate"

    # Reconnect loop — if ComfyUI restarts mid-session, we retry
    while True:
        uri = f"ws://{COMFYUI_HOST}:{COMFYUI_PORT}/ws"
        log.info(f"WebSocket: connecting to {uri}")

        try:
            async with websockets.connect(
                uri,
                additional_headers = headers,
                open_timeout       = 300,
                close_timeout      = 10,
                ping_interval      = 15,
                ping_timeout       = 20,
            ) as comfy_ws:

                log.info("WebSocket: connected to ComfyUI")

                # ── client → comfyui ────────────────────────────────────────
                async def client_to_comfy() -> None:
                    try:
                        async for msg in client_ws.iter_bytes():
                            if msg is not None:
                                await comfy_ws.send(msg)
                    except Exception as exc:
                        log.debug(f"client_to_comfy closed: {exc!r}")

                # ── comfyui → client ────────────────────────────────────────
                async def comfy_to_client() -> None:
                    global _inqueue
                    try:
                        async for msg in comfy_ws:
                            if isinstance(msg, bytes):
                                await client_ws.send_bytes(msg)
                            elif msg is not None:
                                # Parse queue depth from status messages
                                if msg.startswith("{"):
                                    import json
                                    try:
                                        obj = json.loads(msg)
                                        if obj.get("type", "").startswith("crystools.monitor"):
                                            # Suppress noisy perf metrics from logs
                                            pass
                                        elif obj.get("type", "") == "status":
                                            _inqueue = int(
                                                obj["data"]["status"]["exec_info"]["queue_remaining"]
                                            )
                                            log.info(f"Queue remaining: {_inqueue}")
                                    except Exception:
                                        pass
                                await client_ws.send_text(msg)
                    except Exception as exc:
                        log.debug(f"comfy_to_client closed: {exc!r}")

                # ── watchdog — close internal ws when client disconnects ────
                async def watch_disconnect() -> None:
                    while True:
                        if client_ws.client_state == WebSocketState.DISCONNECTED:
                            log.info("Client disconnected — closing ComfyUI WebSocket")
                            if comfy_ws.state != State.CLOSED:
                                await comfy_ws.close()
                            break
                        if comfy_ws.state == State.CLOSED:
                            break
                        await asyncio.sleep(0.1)

                t_c2c   = asyncio.create_task(client_to_comfy())
                t_c2cl  = asyncio.create_task(comfy_to_client())
                t_watch = asyncio.create_task(watch_disconnect())

                done, pending = await asyncio.wait(
                    {t_c2c, t_c2cl, t_watch},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                log.info("WebSocket session closed.")

        except ConnectionClosedError as exc:
            log.warning(f"WebSocket connection closed unexpectedly: {exc!r}")
        except (OSError, Exception) as exc:
            log.warning(f"WebSocket connection failed: {exc!r}")

        # Exit the reconnect loop when the client is gone
        if client_ws.client_state == WebSocketState.DISCONNECTED:
            break

        await asyncio.sleep(1)


# ─────────────────────────────────────────────────────────────────────────────
# Catch-all proxy — ComfyUI static assets, custom node UIs, etc.
# Modal equivalent: proxy() catch-all route
# ─────────────────────────────────────────────────────────────────────────────

@app.api_route(
    "/{path:path}",
    methods=["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE"],
)
async def proxy_catchall(request: Request, path: str):
    headers = _proxy_headers(request)
    headers["accept-encoding"] = "gzip, br, deflate"
    body = await request.body()

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.request(
            method  = request.method,
            url     = f"{COMFYUI_URL}/{path}",
            params  = request.query_params,
            headers = headers,
            content = body,
        )

    return Response(
        content     = resp.content,
        status_code = resp.status_code,
        headers     = dict(resp.headers),
    )
