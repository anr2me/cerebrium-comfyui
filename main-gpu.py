"""
main-gpu.py — Cerebrium entry point based on ComfyGPU class from anr2me/modal-comfyui.

Differences from main.py (which was based on ComfyMix):

  main.py (ComfyMix)                    main-gpu.py (ComfyGPU)
  ─────────────────────────────────     ──────────────────────────────────
  ComfyUI runs with --cpu flag          ComfyUI runs WITHOUT --cpu → GPU
  Has CPU↔GPU routing logic             Direct proxy — always GPU
  Tracks ws_ready, active, inqueue      Tracks active + inqueue only
  Reconnects WS between CPU/GPU         Single WS target (always local)
  pending_prompt cross-container sync   Simplified — single container
  Port: 8188 (uiport)                   Port: 8189 (gpuport, like original)

Modal equivalent:
  ComfyGPU.start_checkpoint()  → lifespan() startup  (snap=True path)
  ComfyGPU.start_restore()     → lifespan() startup  (snap=False path,
                                                       increments active)
  ComfyGPU.web()               → FastAPI ASGI app here
  ComfyGPU.cleanup()           → lifespan() shutdown  (decrements active,
                                                        resets inqueue)

Note on active/inqueue:
  In Modal these were cross-container shared_dict values so ComfyMix could
  see how many GPU instances were live. In a single Cerebrium container there
  is no cross-container routing, so active/inqueue are kept as in-process
  counters and exposed via /api/cerebrium/stats for observability.
"""

from __future__ import annotations

import asyncio
import json
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

# Modal original: gpuport = uiport + 1 = 8188 + 1 = 8189
COMFYUI_PORT     = int(os.getenv("COMFYUI_PORT", "8189"))
COMFYUI_HOST     = "127.0.0.1"
COMFYUI_URL      = f"http://{COMFYUI_HOST}:{COMFYUI_PORT}"

BASE_DIR        = Path("/persistent-storage/cache/ComfyUI/")
USER_DIR        = Path("/persistent-storage/cache/ComfyUI/user")
OUTPUT_DIR      = Path("/persistent-storage/cache/ComfyUI/output")
INPUT_DIR       = Path("/persistent-storage/cache/ComfyUI/input")
TEMP_DIR        = Path("/persistent-storage/cache/ComfyUI/temp")
MODELS_DIR      = Path("/persistent-storage/cache/ComfyUI/models")
CUSNODES_DIR    = Path("/persistent-storage/cache/ComfyUI/custom_nodes")

STARTUP_TIMEOUT  = 300   # Modal: startup_timeout=300
PROXY_TIMEOUT    = 120
WS_OPEN_TIMEOUT  = 300   # Modal: open_timeout=300

# ─────────────────────────────────────────────────────────────────────────────
# State  (mirrors Modal ComfyGPU's shared_dict entries, now in-process)
#
#   shared_dict["active"]   → _active    (how many GPU containers are live)
#   shared_dict["inqueue"]  → _inqueue   (ComfyUI queue depth, from WS status)
# ─────────────────────────────────────────────────────────────────────────────

_comfyui_proc: Optional[subprocess.Popen] = None

_active:  int = 0   # incremented on restore, decremented on cleanup
_inqueue: int = 0   # last known ComfyUI queue_remaining
_pending: int = 0   # prompts in-flight (not yet acked by ComfyUI)

_pending_lock = asyncio.Lock()   # guard _pending  (fixes Modal bug #2)


# ─────────────────────────────────────────────────────────────────────────────
# ComfyUI process management
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_dirs() -> None:
    for d in [USER_DIR / "default/workflows", OUTPUT_DIR, INPUT_DIR, TEMP_DIR, MODELS_DIR, CUSNODES_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def _start_comfyui() -> subprocess.Popen:
    """
    Launch ComfyUI WITHOUT --cpu so it uses the GPU.
    Modal equivalent: ComfyGPU.start_checkpoint() Popen call.
    """
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
        # NOTE: no --cpu flag here — this is the GPU variant
    )
    log.info(f"[GPU] Starting ComfyUI on port {COMFYUI_PORT} (GPU mode): {cmd}")
    proc = subprocess.Popen(
        cmd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    def _stream(p: subprocess.Popen) -> None:
        for line in p.stdout:
            log.info("[ComfyUI-GPU] " + line.decode(errors="replace").rstrip())

    threading.Thread(target=_stream, args=(proc,), daemon=True).start()
    return proc


def _wait_for_port(port: int, timeout: int = STARTUP_TIMEOUT) -> None:
    """
    Block until ComfyUI accepts connections.
    Modal equivalent: wait_for_port(gpuport, timeout=300)
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((COMFYUI_HOST, port), timeout=1):
                log.info(f"[GPU] ComfyUI ready on port {port}.")
                return
        except OSError:
            time.sleep(0.5)
    raise TimeoutError(f"ComfyUI did not become ready on port {port} within {timeout}s")


# ─────────────────────────────────────────────────────────────────────────────
# Lifespan
#
# Modal equivalent:
#   @modal.enter(snap=True)  → first start: launch ComfyUI, wait for port
#   @modal.enter(snap=False) → restore:     increment active, wait for port
#   @modal.exit()            → cleanup:     decrement active, reset inqueue,
#                                           terminate proc
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _comfyui_proc, _active, _inqueue

    log.info("=== [GPU] Cold start: launching ComfyUI (GPU mode) ===")
    _comfyui_proc = _start_comfyui()

    # snap=True path: block until ComfyUI is up (snapshot taken after this)
    _wait_for_port(COMFYUI_PORT, timeout=STARTUP_TIMEOUT)
    log.info("[GPU] ComfyUI ready — snapshot point reached.")

    # snap=False / restore path: increment active counter
    # Modal: shared_dict["active"] = shared_dict.get("active", 0) + 1
    _active += 1
    log.info(f"[GPU] active={_active}")

    log.info("=== [GPU] Accepting requests ===")
    yield

    # ── @modal.exit() equivalent ──────────────────────────────────────────────
    log.info("[GPU] Cleanup: decrementing active counter ...")
    _active  = max(0, _active - 1)
    _inqueue = 0   # no inference running during shutdown (Modal: shared_dict["inqueue"] = 0)
    log.info(f"[GPU] active={_active}, inqueue={_inqueue}")

    if _comfyui_proc and _comfyui_proc.poll() is None:
        log.info("[GPU] Terminating ComfyUI process ...")
        _comfyui_proc.terminate()
        try:
            _comfyui_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _comfyui_proc.kill()
    log.info("=== [GPU] Shutdown complete ===")


app = FastAPI(title="ComfyUI GPU Worker — Cerebrium", lifespan=lifespan)


# ─────────────────────────────────────────────────────────────────────────────
# Health check + stats  (Cerebrium requires /health)
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    if _comfyui_proc is None or _comfyui_proc.poll() is not None:
        return JSONResponse(
            {"status": "error", "detail": "ComfyUI process not running"},
            status_code=503,
        )
    return {"status": "ok", "active": _active, "inqueue": _inqueue, "pending": _pending}


@app.get("/api/cerebrium/stats")
async def stats():
    """
    Exposes the counters that Modal shared_dict tracked cross-container.
    Useful for debugging scaling behaviour.
    """
    return {
        "active":       _active,    # Modal: shared_dict["active"]
        "inqueue":      _inqueue,   # Modal: shared_dict["inqueue"]
        "pending":      _pending,   # Modal: shared_dict["pending_prompt"]
        "comfyui_pid":  _comfyui_proc.pid if _comfyui_proc else None,
        "comfyui_alive": _comfyui_proc is not None and _comfyui_proc.poll() is None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# HTTP proxy helpers
# ─────────────────────────────────────────────────────────────────────────────

_STRIP_HEADERS = frozenset([
    "host", "content-length",
    "x-forwarded-proto", "x-forwarded-for",
    "x-forwarded-host", "x-forwarded-port",
])


def _proxy_headers(request: Request) -> dict:
    h = {k: v for k, v in request.headers.items() if k.lower() not in _STRIP_HEADERS}
    h["accept-encoding"] = "gzip, br, deflate"   # avoid zstd blob (Modal bug #9)
    return h


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
# Prompt / queue  (GPU inference trigger)
#
# pending counter guarded with try/finally — fixes Modal bug #2
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/prompt")
@app.post("/api/prompt")
@app.post("/queue")
@app.post("/api/queue")
async def proxy_prompt(request: Request):
    global _pending
    async with _pending_lock:
        _pending += 1
    log.info(f"[GPU] prompt received (pending={_pending}, active={_active})")

    try:
        target = f"{COMFYUI_URL}{request.url.path}"
        return await _forward(request, target, try_json=True)
    finally:
        async with _pending_lock:
            _pending = max(0, _pending - 1)
        log.info(f"[GPU] prompt forwarded (pending={_pending})")


@app.get("/prompt")
@app.get("/api/prompt")
@app.get("/queue")
@app.get("/api/queue")
async def proxy_queue_get(request: Request):
    return await _forward(request, f"{COMFYUI_URL}{request.url.path}", try_json=True)


# ─────────────────────────────────────────────────────────────────────────────
# Interrupt
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/interrupt")
@app.post("/api/interrupt")
async def proxy_interrupt(request: Request):
    return await _forward(request, f"{COMFYUI_URL}{request.url.path}", try_json=True)


# ─────────────────────────────────────────────────────────────────────────────
# View  (binary output — images, videos)
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/view")
@app.get("/api/view")
async def proxy_view(request: Request):
    return await _forward(request, f"{COMFYUI_URL}{request.url.path}", try_json=False)


# ─────────────────────────────────────────────────────────────────────────────
# Internal logs
# ─────────────────────────────────────────────────────────────────────────────

@app.patch("/internal/logs{path:path}")
@app.get("/internal/logs{path:path}")
async def proxy_logs(request: Request, path: str):
    return await _forward(request, f"{COMFYUI_URL}{request.url.path}", try_json=True)


# ─────────────────────────────────────────────────────────────────────────────
# Other API / internal routes
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/{path:path}")
@app.get("/internal/{path:path}")
async def proxy_api(request: Request, path: str):
    return await _forward(request, f"{COMFYUI_URL}{request.url.path}", try_json=True)


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket proxy
#
# Differences from main.py (ComfyMix):
#   - No CPU<->GPU switching loop — always connects to local ComfyUI GPU port
#   - Reads inqueue from status messages and updates _inqueue counter
#   - Suppresses crystools.monitor noise (same as original)
#   - watch_disconnect watchdog closes internal WS when client disconnects
# ─────────────────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def proxy_websocket(client_ws: WebSocket):
    global _inqueue

    await client_ws.accept()

    headers = {
        k: v for k, v in client_ws.headers.items()
        if k.lower() not in _STRIP_HEADERS
    }
    headers["accept-encoding"] = "gzip, br, deflate"

    uri = f"ws://{COMFYUI_HOST}:{COMFYUI_PORT}/ws"
    log.info(f"[GPU] WebSocket: connecting to {uri}")

    # Reconnect loop — retry if ComfyUI connection drops but client is still connected
    while True:
        try:
            async with websockets.connect(
                uri,
                additional_headers = headers,
                open_timeout       = WS_OPEN_TIMEOUT,  # Modal: open_timeout=300
                close_timeout      = 10,
                ping_interval      = 15,                # Modal: ping_interval=15
                ping_timeout       = 20,                # Modal: ping_timeout=20
            ) as comfy_ws:

                ws_host = comfy_ws.request.headers.get("Host", "")
                log.info(f"[GPU] WebSocket: connected (host={ws_host})")

                # ── client -> comfyui ────────────────────────────────────────
                async def client_to_comfy() -> None:
                    try:
                        async for msg in client_ws.iter_bytes():
                            if msg is not None:
                                log.debug(f"client_to_comfy: {msg!r}")
                                await comfy_ws.send(msg)
                    except Exception as exc:
                        log.debug(f"client_to_comfy closed: {exc!r}")

                # ── comfyui -> client ────────────────────────────────────────
                async def comfy_to_client() -> None:
                    global _inqueue
                    try:
                        async for msg in comfy_ws:
                            # Binary frame (preview images, latents)
                            if isinstance(msg, bytes):
                                log.debug(f"comfy_to_client(b): {len(msg)} bytes")
                                await client_ws.send_bytes(msg)
                                continue

                            if msg is None:
                                continue

                            # Text frame
                            print_msg = True

                            if msg.startswith("{"):
                                try:
                                    obj      = json.loads(msg)
                                    msg_type = obj.get("type", "")

                                    # Suppress noisy perf metrics (same as original)
                                    if msg_type.startswith("crystools.monitor"):
                                        print_msg = False

                                    # Track GPU-side queue depth
                                    # Modal: shared_dict.put("inqueue", queue_remaining)
                                    elif msg_type == "status":
                                        _inqueue = int(
                                            obj["data"]["status"]["exec_info"]["queue_remaining"]
                                        )
                                        log.info(f"[GPU] queue_remaining={_inqueue}")

                                except (json.JSONDecodeError, KeyError):
                                    pass

                            if print_msg:
                                log.debug(f"comfy_to_client: {msg}")

                            await client_ws.send_text(msg)

                    except Exception as exc:
                        log.debug(f"comfy_to_client closed: {exc!r}")

                # ── watchdog: close internal WS when client disconnects ──────
                async def watch_disconnect() -> None:
                    while True:
                        if client_ws.client_state == WebSocketState.DISCONNECTED:
                            log.info("[GPU] Client disconnected — closing ComfyUI WS")
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
                log.info("[GPU] WebSocket session ended.")

        except ConnectionClosedError as exc:
            # Modal: "ConnectionClosedError could mean the GPU container got SIGKILLed"
            log.warning(f"[GPU] WebSocket connection closed unexpectedly: {exc!r}")
        except (OSError, Exception) as exc:
            log.warning(f"[GPU] WebSocket connection failed: {exc!r}")

        if client_ws.client_state == WebSocketState.DISCONNECTED:
            break

        await asyncio.sleep(1)


# ─────────────────────────────────────────────────────────────────────────────
# Catch-all proxy — static assets, custom node UIs, uploads, etc.
# Modal equivalent: ComfyGPU.web() exposes the whole ComfyUI HTTP server.
# ─────────────────────────────────────────────────────────────────────────────

@app.api_route(
    "/{path:path}",
    methods=["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE"],
)
async def proxy_catchall(request: Request, path: str):
    headers = _proxy_headers(request)
    body    = await request.body()

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
