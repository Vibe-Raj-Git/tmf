"""
MoDaaS NAI Governance Agent Server — Phase 2C
Separate server process from modaas_server.py — runs on port 8001 only.
No proxy needed — mock-vendors calls port 8001 directly (Python-to-Python, no CORS).

Start: python servers/modaas_nai_server.py
"""
import asyncio
import sys

# Windows asyncio fix — MUST be first, before any other imports
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import os
from pathlib import Path

# Add modaas-agents root to path so coded_tools are importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Add coded_tools to sys.path so neuro-san can import CodedTool classes
coded_tools_path = str(Path(__file__).parent.parent / "coded_tools")
if coded_tools_path not in sys.path:
    sys.path.insert(0, coded_tools_path)

# Load .env from modaas-agents root
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

# Override manifest and port AFTER load_dotenv
os.environ["AGENT_MANIFEST_FILE"] = str(
    Path(__file__).parent.parent / "registries" / "nai_manifest.hocon"
)
os.environ["AGENT_HTTP_PORT"] = "8001"

from neuro_san.service.main_loop.server_main_loop import ServerMainLoop

if __name__ == "__main__":
    print("[NAI Server] Starting MoDaaS NAI Governance Agent...")
    print(f"   Manifest:         {os.environ.get('AGENT_MANIFEST_FILE')}")
    print(f"   Port:             8001 (direct — no proxy needed)")
    print(f"   Gemini:           {'SET' if os.environ.get('GOOGLE_API_KEY') else 'NOT SET'}")
    print(f"   LLM Router:       {os.environ.get('LLMROUTER_ENDPOINT', 'http://localhost:8002/mock/llmrouter/provision')}")
    print(f"   Neo4j Backend:    {os.environ.get('NEO4J_BACKEND', 'http://localhost:3000')}")
    print(f"   Endpoint:         http://localhost:8001/api/v1/modaas_nai_agent/streaming_chat")
    ServerMainLoop().main_loop()