"""
MoDaaS CORS Proxy
Runs on port 30012 — Angular connects here.
Forwards all requests to Neuro SAN on port 30011 with CORS headers.

NOTE: Despite the endpoint name "streaming_chat", Neuro SAN returns a single
complete JSON object — confirmed via test_stream.py on April 1, 2026.
We use a buffered (non-streaming) approach to avoid asyncio conflicts between
uvicorn (this proxy) and Tornado (Neuro SAN server).
"""

import asyncio
import sys

# Windows asyncio fix — must be set before uvicorn starts its event loop
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

app = FastAPI(title="MoDaaS CORS Proxy")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:4200", "http://127.0.0.1:4200"],
    allow_methods=["*"],
    allow_headers=["*"],
)

NEURO_SAN_BASE = "http://localhost:30011"


@app.api_route("/{path:path}", methods=["GET", "POST", "OPTIONS"])
async def proxy(path: str, request: Request):
    """
    Forward request to Neuro SAN and return buffered response.
    No streaming — Neuro SAN returns a single complete JSON object.
    """
    url = f"{NEURO_SAN_BASE}/{path}"
    body = await request.body()
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length")
    }

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.request(
                method=request.method,
                url=url,
                content=body,
                headers=headers
            )
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type="application/json"
        )

    except httpx.ConnectError:
        return JSONResponse(
            status_code=503,
            content={"error": "Neuro SAN server not reachable on port 30011"}
        )
    except httpx.TimeoutException:
        return JSONResponse(
            status_code=504,
            content={"error": "Neuro SAN request timed out (120s)"}
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Proxy error: {str(e)}"}
        )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=30012, log_level="warning")