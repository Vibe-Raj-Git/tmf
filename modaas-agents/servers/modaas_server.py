"""
MoDaaS Neuro SAN Server + CORS Proxy V16
Runs two servers:
  - Neuro SAN agent server on port 30011 (internal)
  - FastAPI CORS proxy on port 30012 (Angular connects here)

Angular calls port 30012 → proxy forwards to port 30011 → response back to Angular.
This avoids CORS issues since the proxy adds the required headers.

Usage:
    cd C:/Vibe/GameX/modaas-agents
    python servers/modaas_server.py
"""
import asyncio
import sys

# Fix for Python 3.13 on Windows — prevents asyncio event loop crash on second request
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
import os
import sys
import subprocess
from pathlib import Path

# Add modaas-agents root to path so coded_tools are importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Add coded_tools to sys.path so neuro-san can import CodedTool classes
coded_tools_path = str(Path(__file__).parent.parent / "coded_tools")
if coded_tools_path not in sys.path:
    sys.path.insert(0, coded_tools_path)

# Load .env file from modaas-agents root
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from neuro_san.service.main_loop.server_main_loop import ServerMainLoop


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Starting MoDaaS Neuro SAN server...")
    print(f"   Manifest:    {os.environ.get('AGENT_MANIFEST_FILE', 'NOT SET')}")
    print(f"   Tool Path:   {os.environ.get('AGENT_TOOL_PATH', 'NOT SET')}")
    print(f"   Agent Port:  {os.environ.get('AGENT_HTTP_PORT', '30011')} (internal)")
    print(f"   Proxy Port:  30012 (Angular connects here)")
    print(f"   BSS:         {os.environ.get('BSS_ENDPOINT', 'NOT SET')}")
    print("")
    print("   Angular endpoint: http://localhost:30012/api/v1/modaas_customer_agent/streaming_chat")
    print("")

    # Start CORS proxy as a separate process
    proxy_script = str(Path(__file__).parent / "cors_proxy.py")
    proxy_process = subprocess.Popen(
        [sys.executable, proxy_script],
        env=os.environ.copy()
    )
    print(f"CORS proxy started on port 30012 (PID: {proxy_process.pid})...")

    # Start Neuro SAN server (blocking — runs on main thread)
    ServerMainLoop().main_loop()