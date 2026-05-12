#!/usr/bin/env python3
"""
Thin Anthropic-compatible gateway -> Kimi Code.

Sits at http://127.0.0.1:$PORT and forwards /v1/messages and
/v1/messages/count_tokens straight to https://api.kimi.com/coding/, swapping
the Authorization header to the Kimi key and the User-Agent to claude-cli/*.
No protocol translation -- Kimi's /coding/ endpoint already speaks Anthropic
Messages, so thinking blocks survive intact.

Required env:
  KIMI_KEY              sk-kimi-...      (gateway holds the Kimi credential)

Optional env:
  PORT                  default 8765
  KIMI_BASE             default https://api.kimi.com/coding
  UPSTREAM_USER_AGENT   default claude-cli/2.1.139
  UPSTREAM_MODEL        default kimi-for-coding   (rewrites client's "model")
  REWRITE_MODEL         "1"/"0", default "1"      (rewrite enabled)
  BIND_HOST             default 127.0.0.1
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.middleware.base import BaseHTTPMiddleware

KIMI_BASE = os.environ.get("KIMI_BASE", "https://api.kimi.com/coding").rstrip("/")
KIMI_KEY = os.environ.get("KIMI_KEY", "").strip()
PORT = int(os.environ.get("PORT", "8765"))
BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")
UPSTREAM_USER_AGENT = os.environ.get("UPSTREAM_USER_AGENT", "claude-cli/2.1.139")
UPSTREAM_MODEL = os.environ.get("UPSTREAM_MODEL", "kimi-for-coding")
REWRITE_MODEL = os.environ.get("REWRITE_MODEL", "1") not in ("0", "false", "False", "")
SSL_CERTFILE = os.environ.get("SSL_CERTFILE", "").strip()
SSL_KEYFILE = os.environ.get("SSL_KEYFILE", "").strip()

if not KIMI_KEY:
    print("ERROR: KIMI_KEY env var is required (sk-kimi-...).", file=sys.stderr)
    sys.exit(1)

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
    "content-encoding",
}
STRIP_FROM_CLIENT = HOP_BY_HOP | {
    "authorization", "x-api-key", "user-agent",
    "accept-encoding",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    timeout = httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0)
    app.state.client = httpx.AsyncClient(timeout=timeout, http2=False)
    scheme = "https" if (SSL_CERTFILE and SSL_KEYFILE) else "http"
    print(
        f"[kimi-gateway] listening on {scheme}://{BIND_HOST}:{PORT}\n"
        f"[kimi-gateway] upstream  : {KIMI_BASE}\n"
        f"[kimi-gateway] user-agent: {UPSTREAM_USER_AGENT}\n"
        f"[kimi-gateway] model     : {UPSTREAM_MODEL} (rewrite={REWRITE_MODEL})",
        flush=True,
    )
    try:
        yield
    finally:
        await app.state.client.aclose()


app = FastAPI(lifespan=lifespan, openapi_url=None, docs_url=None, redoc_url=None)


class PrivateNetworkAccessMiddleware(BaseHTTPMiddleware):
    """Chromium-based browsers (incl. Edge WebView used by some Office hosts)
    require Access-Control-Allow-Private-Network: true when a public-origin
    page fetches a private/loopback target. WebKit also tolerates it.

    We mirror the request's Origin in Access-Control-Allow-Origin so that
    sites which send credentials still validate, and we add the PNA header
    on both preflight and actual responses.
    """

    async def dispatch(self, request: Request, call_next):
        origin = request.headers.get("origin")
        is_pna_preflight = (
            request.method == "OPTIONS"
            and request.headers.get("access-control-request-private-network") == "true"
        )
        if is_pna_preflight:
            req_method = request.headers.get("access-control-request-method", "POST")
            req_headers = request.headers.get(
                "access-control-request-headers",
                "authorization, content-type, x-api-key, anthropic-version, anthropic-beta",
            )
            return Response(
                status_code=204,
                headers={
                    "Access-Control-Allow-Origin": origin or "*",
                    "Access-Control-Allow-Methods": f"{req_method}, GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers": req_headers,
                    "Access-Control-Allow-Private-Network": "true",
                    "Access-Control-Max-Age": "86400",
                    "Vary": "Origin",
                },
            )
        response = await call_next(request)
        if origin:
            response.headers["Access-Control-Allow-Private-Network"] = "true"
        return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=86400,
)
# Add PNA last so it is the OUTERMOST middleware (Starlette runs middleware
# in reverse registration order). PNA must see OPTIONS requests before
# CORSMiddleware, since CORSMiddleware rejects unknown PNA preflights.
app.add_middleware(PrivateNetworkAccessMiddleware)


def build_upstream_headers(request: Request) -> dict[str, str]:
    """Copy client headers we want to forward, then overlay our auth + UA."""
    out: dict[str, str] = {}
    for k, v in request.headers.items():
        if k.lower() in STRIP_FROM_CLIENT:
            continue
        out[k] = v
    out["Authorization"] = f"Bearer {KIMI_KEY}"
    out["User-Agent"] = UPSTREAM_USER_AGENT
    # Ensure anthropic-version present -- some clients (Claude Desktop, third-party)
    # rely on the gateway to inject a default. Use the current public version.
    out.setdefault("anthropic-version", "2023-06-01")
    return out


def maybe_rewrite_body(body: bytes) -> bytes:
    if not REWRITE_MODEL or not body:
        return body
    try:
        data = json.loads(body)
    except (ValueError, json.JSONDecodeError):
        return body
    if isinstance(data, dict) and "model" in data and data.get("model") != UPSTREAM_MODEL:
        data["model"] = UPSTREAM_MODEL
        return json.dumps(data, ensure_ascii=False).encode("utf-8")
    return body


def is_streaming(body: bytes) -> bool:
    if not body:
        return False
    try:
        return bool(json.loads(body).get("stream"))
    except Exception:
        return False


def filter_response_headers(headers: httpx.Headers) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in headers.items():
        if k.lower() in HOP_BY_HOP:
            continue
        out[k] = v
    return out


async def proxy(request: Request, path: str) -> Response:
    body = maybe_rewrite_body(await request.body())
    headers = build_upstream_headers(request)
    query = ("?" + request.url.query) if request.url.query else ""
    url = f"{KIMI_BASE}{path}{query}"
    client: httpx.AsyncClient = app.state.client

    if is_streaming(body):
        upstream = client.build_request("POST", url, content=body, headers=headers)
        resp = await client.send(upstream, stream=True)

        # If upstream errored, drain and return a normal (non-stream) response so
        # the client gets a useful body instead of a half-open SSE stream.
        if resp.status_code >= 400:
            raw = await resp.aread()
            await resp.aclose()
            return Response(
                content=raw,
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type", "application/json"),
                headers=filter_response_headers(resp.headers),
            )

        async def stream_iter() -> AsyncIterator[bytes]:
            try:
                async for chunk in resp.aiter_raw():
                    yield chunk
            finally:
                await resp.aclose()

        return StreamingResponse(
            stream_iter(),
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "text/event-stream"),
            headers=filter_response_headers(resp.headers),
        )

    resp = await client.post(url, content=body, headers=headers)
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
        headers=filter_response_headers(resp.headers),
    )


# --- Anthropic Messages endpoints ----------------------------------------

@app.post("/v1/messages")
async def messages(request: Request) -> Response:
    return await proxy(request, "/v1/messages")


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request) -> Response:
    return await proxy(request, "/v1/messages/count_tokens")


# --- Gateway probes ------------------------------------------------------

@app.get("/healthz")
@app.get("/health")
async def healthz() -> dict:
    return {"status": "ok"}


@app.get("/")
async def root() -> dict:
    return {
        "service": "kimi-gateway",
        "upstream": KIMI_BASE,
        "model": UPSTREAM_MODEL,
    }


@app.get("/v1/models")
async def list_models() -> JSONResponse:
    """Return claude-prefixed aliases so Claude Code's model discovery picks
    them up (it filters /v1/models to ids starting with 'claude' or
    'anthropic'). All aliases route to the same underlying kimi-for-coding."""
    now = int(time.time())
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    data = [
        {"type": "model", "id": "claude-opus-4-1", "display_name": "Kimi For Coding (opus alias)", "created_at": iso},
        {"type": "model", "id": "claude-sonnet-4-5", "display_name": "Kimi For Coding (sonnet alias)", "created_at": iso},
        {"type": "model", "id": "claude-haiku-4-5", "display_name": "Kimi For Coding (haiku alias)", "created_at": iso},
        {"type": "model", "id": "kimi-for-coding", "display_name": "Kimi For Coding", "created_at": iso},
    ]
    return JSONResponse({"data": data, "has_more": False, "first_id": data[0]["id"], "last_id": data[-1]["id"]})


# --- Anthropic-format error handler --------------------------------------

@app.exception_handler(Exception)
async def anthropic_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={
            "type": "error",
            "error": {"type": "api_error", "message": str(exc)},
            "request_id": f"req_{uuid.uuid4().hex[:24]}",
        },
    )


if __name__ == "__main__":
    kwargs = dict(
        host=BIND_HOST,
        port=PORT,
        log_level="warning",
        access_log=False,
    )
    if SSL_CERTFILE and SSL_KEYFILE:
        kwargs["ssl_certfile"] = SSL_CERTFILE
        kwargs["ssl_keyfile"] = SSL_KEYFILE
    uvicorn.run("server:app", **kwargs)
