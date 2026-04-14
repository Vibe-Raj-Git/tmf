"""
MoDaaS Mock Vendor Server — Phase 2C (complete)
Simulates THIRD-PARTY vendors only: BSS (Etiya), OSS (Ni2), TC (Cisco), LLM Router
Port: 8002

Fulfillment pipeline (matches sequence diagram exactly):
  Step 1: BSS acknowledges → pushes serviceAccessToken to LLM Router (S2S)
  Step 2: OSS → NAI: get TMF915 rules → saves rules_{id}.json
          OSS → TC: feasibility check
          OSS: digital twin simulation (1s)
  Step 3: OSS → NAI: full validation → NAI pushes sovereigntyToken to LLM Router
          NAI → Neo4j: audit entry
  Step 4: TC assigns Router_IP → OSS pushes Router_IP to LLM Router (ACTIVE)

LLM Router binding lifecycle:
  BSS_PROVISIONED  → serviceAccessToken stored (Step 1)
  PENDING          → sovereigntyToken added (Step 3)
  ACTIVE           → Router_IP added (Step 4)

Gate 2 at inference time:
  Gate 2a: LLM Router checks own store for serviceAccessToken (no BSS call)
  Gate 2b: LLM Router calls GET /mock/bss/balance/{id} (credit check only)

Token economy:
  Starting balance per champion (set on ACKNOWLEDGED)
  Deducted per inference call (POST /mock/bss/deduct)
  Rechargeable via POST /mock/bss/recharge

Start: uvicorn main:app --port 8002
"""

import ast
import asyncio
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(__file__).parent.parent / "modaas-agents" / ".env")

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import re as _re

def _to_float(val, default: float = 999.0) -> float:
    """Robustly extract first numeric value from any LLM output format.
    Handles: '9', 'less than 9ms', '9ms', '< 9', '3 dollars per MTok', '$3/MTok'
    """
    match = _re.search(r'[\d.]+', str(val))
    return float(match.group()) if match else default

app = FastAPI(title="MoDaaS Mock Vendors", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:4200", "http://127.0.0.1:4200"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Config ────────────────────────────────────────────────────────────────────
NEO4J_BACKEND  = os.environ.get("NEO4J_BACKEND",  "http://localhost:3000")
NAI_ENDPOINT   = os.environ.get("NAI_ENDPOINT",   "http://localhost:8001")
PLANNING_DELAY = int(os.environ.get("PLANNING_DELAY", "3"))
ACTIVE_DELAY   = int(os.environ.get("ACTIVE_DELAY",   "10"))

# ── Starting token balances per champion org ──────────────────────────────────
CHAMPION_BALANCES = {
    "colt":        50_000,
    "stc":         75_000,
    "telefonica":  60_000,
    "telefónica":  60_000,
    "turk":        45_000,
    "türk":        45_000,
    "verizon":     80_000,
}
DEFAULT_BALANCE = 10_000

def get_starting_balance(org_name: str) -> int:
    name = (org_name or "").lower()
    for key, bal in CHAMPION_BALANCES.items():
        if key in name:
            return bal
    return DEFAULT_BALANCE

# ── Persistence ───────────────────────────────────────────────────────────────
STORE_DIR      = Path(__file__).parent
ORDERS_FILE    = STORE_DIR / "orders.json"
LLMROUTER_FILE = STORE_DIR / "llmrouter.json"
RULES_DIR      = STORE_DIR

def _load(path: Path) -> Dict:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            print(f"[Store] Loaded {len(data)} records from {path.name}")
            return data
    except Exception as e:
        print(f"[Store] Could not load {path.name} — starting empty ({e})")
    return {}

def _save(path: Path, data: Dict) -> None:
    try:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[Store] Could not save {path.name} — {e}")

orders: Dict[str, Any]          = _load(ORDERS_FILE)
llmrouter_store: Dict[str, Any] = _load(LLMROUTER_FILE)

def persist() -> None:
    _save(ORDERS_FILE,    orders)
    _save(LLMROUTER_FILE, llmrouter_store)

def save_rules(intent_id: str, rules: Dict) -> None:
    path = RULES_DIR / f"rules_{intent_id[:8]}.json"
    try:
        path.write_text(json.dumps({
            "intent_id":  intent_id,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "source":     "NAI-AGENT" if rules.get("source") == "NAI-AGENT" else "STUB",
            "rules":      rules
        }, indent=2), encoding="utf-8")
        print(f"[OSS] TMF915 rules saved to {path.name}")
    except Exception as e:
        print(f"[OSS] Could not save rules file: {e}")

# ── Neo4j helper ──────────────────────────────────────────────────────────────
async def write_to_neo4j(path: str, payload: Dict) -> None:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(f"{NEO4J_BACKEND}{path}", json=payload)
    except Exception as e:
        print(f"[Neo4j write skipped] {path} — {e}")

# ── Internet exchange lookup ──────────────────────────────────────────────────
def get_exchange(location: str) -> str:
    loc = location.upper()
    if any(x in loc for x in ["GERMANY", "FRANKFURT", "BERLIN", "MUNICH"]):
        return "DE-CIX Frankfurt"
    if any(x in loc for x in ["UK", "LONDON", "UNITED KINGDOM"]):
        return "LINX London"
    if any(x in loc for x in ["FRANCE", "PARIS"]):
        return "France-IX Paris"
    if any(x in loc for x in ["SPAIN", "MADRID"]):
        return "ESPANIX Madrid"
    if any(x in loc for x in ["TURKEY", "ISTANBUL", "ANKARA"]):
        return "TREX Istanbul"
    if any(x in loc for x in ["UAE", "DUBAI"]):
        return "Dubai Internet Exchange (DIX)"
    if any(x in loc for x in ["SAUDI", "RIYADH"]):
        return "SAIX Riyadh"
    return "Regional Internet Exchange"

# ─────────────────────────────────────────────────────────────────────────────
# NAI Governance Agent calls
# ─────────────────────────────────────────────────────────────────────────────

# Stub rule definitions — returned when NAI not running
STUB_RULES = {
    "RULE-001": {"description": "Location must be GCC or EU zone",           "threshold": "GCC or EU"},
    "RULE-002": {"description": "Green score >= 3/5",                        "threshold": 3},
    "RULE-003": {"description": "Data training must be disabled",            "threshold": "no-data-training"},
    "RULE-004": {"description": "Inference latency < 10ms",                  "threshold": 10},
    "RULE-005": {"description": "Input <= $3/MTok, Output <= $20/MTok",      "threshold": {"input": 3.0, "output": 20.0}},
    "source":   "STUB"
}


def _parse_nai_response(data: dict) -> dict:
    """
    Extract NAIGovernanceTool result from Neuro SAN's response structure.

    Neuro SAN wraps the CodedTool result as a Python repr string in:
      response.chat_context.chat_histories[0].messages[1].text

    The text uses {{double braces}} for inner dicts (Python format string
    escaping). We unescape these before parsing.

    Parsing strategy:
      1. Unescape {{double braces}}
      2. Try ast.literal_eval() — works for simple dicts
      3. Fallback to json.loads() after replacing single quotes —
         handles deeply nested dicts that ast fails on
    Falls back to empty dict if all parsing fails.
    """
    try:
        histories = (data.get("response", {})
                        .get("chat_context", {})
                        .get("chat_histories", []))
        if not histories:
            return {}
        messages = histories[0].get("messages", [])
        for msg in messages:
            if msg.get("type") == "AI" and msg.get("text", "").strip():
                text = msg["text"].strip()
                # Step 1 — unescape double braces
                text = text.replace("{{", "{").replace("}}", "}")
                # Step 2 — try ast.literal_eval (handles True/False/None)
                try:
                    return ast.literal_eval(text)
                except Exception:
                    pass
                # Step 3 — fallback: convert Python repr to JSON
                # Replace Python bool/None literals with JSON equivalents
                import re
                json_text = text
                json_text = re.sub(r'\bTrue\b',  'true',  json_text)
                json_text = re.sub(r'\bFalse\b', 'false', json_text)
                json_text = re.sub(r'\bNone\b',  'null',  json_text)
                # Replace single quotes with double quotes (carefully)
                json_text = re.sub(r"(?<!\\)'", '"', json_text)
                try:
                    return json.loads(json_text)
                except Exception:
                    pass
    except Exception as e:
        print(f"[NAI parse] Failed to parse response: {e}")
    return {}


async def call_nai_get_rules(intent_id: str) -> Dict:
    """
    Step 2 — OSS asks NAI for TMF915 rule definitions.
    Result saved to rules_{intent_id}.json for audit/debug.
    """
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{NAI_ENDPOINT}/api/v1/modaas_nai_agent/streaming_chat",
                json={
                    "user_message": {
                        "type": "HUMAN",
                        "text": f"Get TMF915 sovereign rules for intent {intent_id}. Return rule definitions only."
                    },
                    "chat_filter": {"chat_filter_type": "MINIMAL"}
                }
            )
        parsed = _parse_nai_response(resp.json())
        rules  = parsed.get("rules", STUB_RULES)
        rules["source"] = "NAI-AGENT"
        print(f"[OSS→NAI] TMF915 rules fetched for intent {intent_id} (NAI-AGENT)")
        return rules
    except Exception as e:
        print(f"[OSS→NAI] NAI not available for rules fetch ({e}) — using stub")
        return STUB_RULES


async def call_nai_validate(intent_id: str, tmf921: Dict) -> Dict:
    """
    Step 3 — OSS submits validated design to NAI for full TMF915 governance.
    NAI validates, generates sovereigntyToken, pushes to LLM Router, writes Neo4j.
    Returns: { validated, compliance_key, rules_passed, source }
    """
    EUR_TO_USD      = 1.08
    modaas_params   = tmf921.get("x-modaas-intentParameters", {})
    constraints     = modaas_params.get("constraints", {})
    green_score_raw  = constraints.get("sustainability", {}).get("minGreenScore", 0)
    green_score      = int(_to_float(green_score_raw, default=0))
    latency          = _to_float(constraints.get("qos", {}).get("latency", 999))
    confidentiality = constraints.get("llm", {}).get("confidentiality", "")
    sovereignty_zone = tmf921.get("x-modaas-applicability", {}).get("sovereigntyZone", "")

    token_prices = constraints.get("tokenPrices", {})
    price_unit   = (token_prices.get("unit") or "USD/MTok").upper()
    input_raw    = _to_float(token_prices.get("inputMax",  999))
    output_raw   = _to_float(token_prices.get("outputMax", 999))

    if "EUR" in price_unit:
        input_usd  = round(input_raw  * EUR_TO_USD, 4)
        output_usd = round(output_raw * EUR_TO_USD, 4)
        print(f"[OSS→NAI] EUR→USD: input {input_raw}€→${input_usd}, output {output_raw}€→${output_usd}")
    else:
        input_usd  = input_raw
        output_usd = output_raw

    stub_key  = "NAI-STUB-" + hashlib.sha256(intent_id.encode()).hexdigest()[:16].upper()
    stub_resp = {
        "validated":      True,
        "compliance_key": stub_key,
        "rules_passed":   {f"RULE-00{i}": True for i in range(1, 6)},
        "source":         "STUB"
    }

    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.post(
                f"{NAI_ENDPOINT}/api/v1/modaas_nai_agent/streaming_chat",
                json={
                    "user_message": {
                        "type": "HUMAN",
                        "text": (
                            f"Validate TMF915 compliance for intent {intent_id}. "
                            f"Sovereignty zone: {sovereignty_zone}. "
                            f"Green score: {green_score}. "
                            f"Latency: {latency}ms. "
                            f"Confidentiality: {confidentiality}. "
                            f"Input price: {input_usd}/MTok. "
                            f"Output price: {output_usd}/MTok. "
                            f"Proceed with full validation, generate compliance key, "
                            f"sync with LLM Router, and write audit entry."
                        )
                    },
                    "chat_filter": {"chat_filter_type": "MINIMAL"}
                }
            )
        parsed         = _parse_nai_response(resp.json())
        compliance_key = parsed.get("compliance_key", stub_key)
        validated      = parsed.get("validated", True)
        rules_passed   = parsed.get("rules_passed", stub_resp["rules_passed"])
        source         = parsed.get("source", "NAI-AGENT")

        print(f"[OSS→NAI] Validation complete for {intent_id} ({source}) — key: {compliance_key[:20]}...")
        return {
            "validated":      validated,
            "compliance_key": compliance_key,
            "rules_passed":   rules_passed,
            "source":         source
        }
    except Exception as e:
        print(f"[OSS→NAI] NAI not available for validation ({e}) — using stub")
        return stub_resp


# ─────────────────────────────────────────────────────────────────────────────
# Fulfillment pipeline — matches sequence diagram exactly
# ─────────────────────────────────────────────────────────────────────────────
async def run_fulfillment(intent_id: str, tmf921: Dict) -> None:
    print(f"\n[Fulfillment] Starting pipeline for intent {intent_id}")

    # ── Step 1b: BSS → LLM Router: push serviceAccessToken (S2S) ─────────────
    api_key = orders[intent_id].get("api_key")
    llmrouter_store[intent_id] = {
        "intent_id":          intent_id,
        "serviceAccessToken": api_key,
        "sovereigntyToken":   None,
        "router_ip":          None,
        "status":             "BSS_PROVISIONED",
        "provisioned_at":     datetime.now(timezone.utc).isoformat(),
        "nai_source":         None
    }
    persist()
    print(f"[BSS→LLMR] serviceAccessToken pushed for {intent_id} — status: BSS_PROVISIONED")

    # ── Step 2: OSS Planning ──────────────────────────────────────────────────
    await asyncio.sleep(PLANNING_DELAY)
    orders[intent_id]["status"]  = "PLANNING"
    plan_id = f"plan-{intent_id[:8]}-001"
    orders[intent_id]["plan_id"] = plan_id
    persist()
    print(f"[OSS] {intent_id} → PLANNING (plan: {plan_id})")

    await write_to_neo4j("/api/audit/status", {
        "intent_id": intent_id, "status": "PLANNING",
        "plan_id": plan_id, "timestamp": datetime.now(timezone.utc).isoformat()
    })

    # ── Step 2a: OSS → NAI: get TMF915 rules ─────────────────────────────────
    print(f"[OSS→NAI] Fetching TMF915 rules for intent {intent_id}...")
    rules = await call_nai_get_rules(intent_id)
    save_rules(intent_id, rules)

    # ── Step 2b: OSS → TC: feasibility check ─────────────────────────────────
    modaas_params = tmf921.get("x-modaas-intentParameters", {})
    source        = modaas_params.get("entryPoint", {}).get("location", "Riyadh")
    target        = modaas_params.get("constraints", {}).get("targetLocation", "Dubai")
    exchange      = get_exchange(target)
    path          = f"{source} → {exchange} → {target}"
    print(f"[OSS→TC] Feasibility check: {path}")

    # ── Step 2c: OSS: Digital Twin simulation ────────────────────────────────
    await asyncio.sleep(1)
    print(f"[OSS] Digital Twin simulation complete for intent {intent_id} — virtual network validated")

    # ── Step 3: OSS → NAI: full validation ───────────────────────────────────
    print(f"[OSS→NAI] Submitting validated design for TMF915 governance — intent {intent_id}...")
    nai_result     = await call_nai_validate(intent_id, tmf921)
    compliance_key = nai_result["compliance_key"]
    validated      = nai_result["validated"]
    rules_passed   = nai_result["rules_passed"]

    orders[intent_id]["compliance_key"] = compliance_key
    orders[intent_id]["nai_rules"]      = rules_passed
    orders[intent_id]["nai_validated"]  = validated
    orders[intent_id]["nai_source"]     = nai_result["source"]

    llmrouter_store[intent_id]["sovereigntyToken"] = compliance_key
    llmrouter_store[intent_id]["status"]           = "PENDING"
    llmrouter_store[intent_id]["nai_source"]       = nai_result["source"]
    persist()
    print(f"[NAI→LLMR] sovereigntyToken pushed for {intent_id} — status: PENDING ({nai_result['source']})")

    await write_to_neo4j("/api/audit/nai", {
        "intent_id":              intent_id,
        "rules_passed":           rules_passed,
        "compliance_key":         compliance_key,
        "sovereignty_token_hint": compliance_key[:12],
        "validated":              validated,
        "source":                 nai_result["source"],
        "timestamp":              datetime.now(timezone.utc).isoformat()
    })

    if not validated:
        orders[intent_id]["status"] = "REJECTED"
        failed = [k for k, v in rules_passed.items() if not v]
        persist()
        print(f"[NAI] {intent_id} → REJECTED (rules failed: {failed})")
        await write_to_neo4j("/api/audit/status", {
            "intent_id": intent_id, "status": "REJECTED",
            "reason": f"TMF915 rules failed: {failed}",
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
        return

    # ── Step 3b: OSS → BSS: READY TO DEPLOY ──────────────────────────────────
    print(f"[OSS→BSS] Intent {intent_id} — READY TO DEPLOY")

    # ── Step 4: TC assigns Router_IP, OSS pushes to LLM Router ───────────────
    await asyncio.sleep(2)
    router_ip = f"10.{uuid.uuid4().int % 256}.{uuid.uuid4().int % 256}.1"

    orders[intent_id]["network_path"] = path
    print(f"[TC] Slice active — Router_IP: {router_ip}, path: {path}")

    await write_to_neo4j("/api/audit/path", {
        "intent_id": intent_id, "path": path,
        "status": "FEASIBLE", "timestamp": datetime.now(timezone.utc).isoformat()
    })

    llmrouter_store[intent_id]["router_ip"]    = router_ip
    llmrouter_store[intent_id]["status"]       = "ACTIVE"
    llmrouter_store[intent_id]["activated_at"] = datetime.now(timezone.utc).isoformat()
    persist()
    print(f"[OSS→LLMR] Router_IP pushed for {intent_id} — binding ACTIVE "
          f"(serviceAccessToken ✅ sovereigntyToken ✅ Router_IP ✅)")

    # ── Step 4c: BSS marks intent ACTIVE ─────────────────────────────────────
    remaining = ACTIVE_DELAY - PLANNING_DELAY - 4
    if remaining > 0:
        await asyncio.sleep(remaining)

    activated_at = datetime.now(timezone.utc).isoformat()
    orders[intent_id]["status"]       = "ACTIVE"
    orders[intent_id]["router_ip"]    = router_ip
    orders[intent_id]["activated_at"] = activated_at
    persist()
    print(f"[BSS] {intent_id} → ACTIVE")

    await write_to_neo4j("/api/audit/status", {
        "intent_id":    intent_id,
        "status":       "ACTIVE",
        "activated_at": activated_at,
        "timestamp":    activated_at
    })


# ─────────────────────────────────────────────────────────────────────────────
# BSS (Etiya) endpoints
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/mock/bss/intent")
async def bss_receive_intent(tmf921: Dict):
    intent_id  = tmf921.get("id", str(uuid.uuid4()))
    intent_ref = f"BSS-REF-{intent_id[:8].upper()}"
    api_key    = f"BSS-KEY-{uuid.uuid4().hex[:12].upper()}"
    created_at = datetime.now(timezone.utc).isoformat()
    org_name   = tmf921.get("relatedParty", [{}])[0].get("name", "")

    orders[intent_id] = {
        "intent_id":           intent_id,
        "intent_ref":          intent_ref,
        "api_key":             api_key,
        "status":              "ACKNOWLEDGED",
        "tmf921":              tmf921,
        "created_at":          created_at,
        "org_name":            org_name,
        "customer_id":         tmf921.get("x-modaas-applicability", {}).get("customerId", ""),
        "service_type":        tmf921.get("x-modaas-applicability", {}).get("targetService", ""),
        "token_balance":       get_starting_balance(org_name),
        "token_balance_start": get_starting_balance(org_name),
        "tokens_used":         0,
        "last_recharge":       None,
        "token_log":           []
    }
    persist()

    await write_to_neo4j("/api/audit/intent", {
        "intent_id":    intent_id,   "intent_ref":   intent_ref,
        "status":       "ACKNOWLEDGED",
        "org_name":     org_name,
        "customer_id":  orders[intent_id]["customer_id"],
        "service_type": orders[intent_id]["service_type"],
        "tmf921":       tmf921,      "timestamp":    created_at
    })

    asyncio.create_task(run_fulfillment(intent_id, tmf921))
    print(f"[BSS] Intent {intent_id} ACKNOWLEDGED → {intent_ref} "
          f"(token balance: {orders[intent_id]['token_balance']:,})")

    return {
        "intentRef": intent_ref,
        "state":     "acknowledged",
        "intent_id": intent_id,
        "message":   f"Intent {intent_ref} received and queued for fulfillment."
    }


@app.get("/mock/bss/intent/{intent_id}")
async def bss_get_intent_status(intent_id: str):
    order = orders.get(intent_id)
    if not order:
        raise HTTPException(status_code=404, detail=f"Intent {intent_id} not found")
    return {
        "intent_id":           order["intent_id"],
        "intent_ref":          order["intent_ref"],
        "status":              order["status"],
        "org_name":            order["org_name"],
        "service_type":        order["service_type"],
        "network_path":        order.get("network_path"),
        "plan_id":             order.get("plan_id"),
        "created_at":          order["created_at"],
        "activated_at":        order.get("activated_at"),
        "token_balance":       order.get("token_balance"),
        "token_balance_start": order.get("token_balance_start"),
        "tokens_used":         order.get("tokens_used", 0),
        "last_recharge":       order.get("last_recharge"),
    }


@app.get("/mock/bss/orders")
async def bss_list_orders():
    return {
        "orders": [
            {
                "intent_id":           o["intent_id"],
                "intent_ref":          o["intent_ref"],
                "status":              o["status"],
                "org_name":            o["org_name"],
                "service_type":        o["service_type"],
                "created_at":          o["created_at"],
                "token_balance":       o.get("token_balance"),
                "token_balance_start": o.get("token_balance_start"),
                "tokens_used":         o.get("tokens_used", 0),
            }
            for o in orders.values()
        ]
    }


@app.get("/mock/bss/balance/{intent_id}")
async def bss_get_balance(intent_id: str):
    order = orders.get(intent_id)
    if not order:
        return {"authorized": False, "reason": "Intent not found"}
    if order.get("status") != "ACTIVE":
        return {"authorized": False, "reason": f"Intent not ACTIVE — status: {order.get('status')}"}
    balance = order.get("token_balance", 0)
    if balance <= 0:
        return {"authorized": False, "reason": f"Insufficient tokens — balance: {balance}"}
    start = order.get("token_balance_start", DEFAULT_BALANCE)
    pct   = round((balance / start) * 100, 1) if start > 0 else 0
    return {
        "authorized":          True,
        "intent_id":           intent_id,
        "token_balance":       balance,
        "token_balance_start": start,
        "token_balance_pct":   pct,
        "low_balance":         pct < 20.0,
        "org_name":            order.get("org_name", ""),
        "message":             "Account active — tokens sufficient"
    }


@app.post("/mock/bss/deduct")
async def bss_deduct_tokens(payload: Dict):
    intent_id   = payload.get("intent_id")
    tokens_used = int(payload.get("tokens_used", 0))
    query_hint  = (payload.get("query_hint") or "")[:40]
    order       = orders.get(intent_id)
    if not order:
        raise HTTPException(status_code=404, detail="Intent not found")
    prev_balance = order.get("token_balance", 0)
    new_balance  = max(0, prev_balance - tokens_used)
    order["token_balance"]  = new_balance
    order["tokens_used"]    = order.get("tokens_used", 0) + tokens_used
    order["token_log"].append({
        "query_hint":    query_hint,
        "tokens_used":   tokens_used,
        "balance_after": new_balance,
        "timestamp":     datetime.now(timezone.utc).isoformat()
    })
    persist()
    start = order.get("token_balance_start", DEFAULT_BALANCE)
    pct   = round((new_balance / start) * 100, 1) if start > 0 else 0
    print(f"[BSS] Tokens deducted for {intent_id}: -{tokens_used} → balance: {new_balance:,} ({pct}%)")
    return {
        "intent_id":         intent_id,
        "tokens_deducted":   tokens_used,
        "previous_balance":  prev_balance,
        "remaining_balance": new_balance,
        "tokens_used_total": order["tokens_used"],
        "token_balance_pct": pct,
        "low_balance":       pct < 20.0,
        "status":            "ok"
    }


@app.post("/mock/bss/recharge")
async def bss_recharge_tokens(payload: Dict):
    intent_id    = payload.get("intent_id")
    amount       = int(payload.get("amount", 0))
    recharged_by = payload.get("recharged_by", "Operator")
    order        = orders.get(intent_id)
    if not order:
        raise HTTPException(status_code=404, detail="Intent not found")
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Recharge amount must be > 0")
    prev_balance = order.get("token_balance", 0)
    new_balance  = prev_balance + amount
    timestamp    = datetime.now(timezone.utc).isoformat()
    order["token_balance"] = new_balance
    order["last_recharge"] = {
        "amount":        amount,
        "timestamp":     timestamp,
        "recharged_by":  recharged_by,
        "balance_after": new_balance
    }
    persist()
    start = order.get("token_balance_start", DEFAULT_BALANCE)
    pct   = round((new_balance / start) * 100, 1) if start > 0 else 0
    print(f"[BSS] Token recharge for {intent_id}: +{amount:,} → balance: {new_balance:,} ({pct}%)")
    return {
        "intent_id":         intent_id,
        "previous_balance":  prev_balance,
        "amount_added":      amount,
        "new_balance":       new_balance,
        "token_balance_pct": pct,
        "low_balance":       pct < 20.0,
        "last_recharge":     order["last_recharge"],
        "message":           f"Token balance recharged successfully. New balance: {new_balance:,}"
    }


@app.get("/mock/bss/token-log/{intent_id}")
async def bss_token_log(intent_id: str):
    order = orders.get(intent_id)
    if not order:
        raise HTTPException(status_code=404, detail="Intent not found")
    return {
        "intent_id":           intent_id,
        "token_balance":       order.get("token_balance"),
        "token_balance_start": order.get("token_balance_start"),
        "tokens_used_total":   order.get("tokens_used", 0),
        "last_recharge":       order.get("last_recharge"),
        "log":                 order.get("token_log", [])
    }


# ─────────────────────────────────────────────────────────────────────────────
# OSS (Ni2) endpoints
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/mock/oss/plan")
async def oss_plan(payload: Dict):
    intent_id = payload.get("intent_id", "unknown")
    await asyncio.sleep(1)
    return {
        "intent_id": intent_id,
        "plans": [
            {"plan_id": f"plan-{intent_id[:8]}-001", "description": "Direct corridor — no gaps",
             "latency_ms": 8, "cost_index": 1.2, "gaps": 0},
            {"plan_id": f"plan-{intent_id[:8]}-002", "description": "Alternate route — 2 handoff points",
             "latency_ms": 12, "cost_index": 0.9, "gaps": 2}
        ],
        "selected_plan": f"plan-{intent_id[:8]}-001",
        "reason":        "Lowest latency, no gaps — meets 10ms SLA"
    }


# ─────────────────────────────────────────────────────────────────────────────
# TC (Cisco) endpoints
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/mock/tc/feasibility")
async def tc_feasibility(payload: Dict):
    intent_id = payload.get("intent_id", "unknown")
    await asyncio.sleep(1)
    return {
        "intent_id":                intent_id,
        "status":                   "FEASIBLE",
        "latency_ms":               7,
        "bandwidth_available_gbps": 10,
        "message":                  "Path confirmed feasible. Meets all QoS constraints."
    }


@app.post("/mock/tc/pathupdate")
async def tc_path_update(payload: Dict):
    intent_id = payload.get("intent_id")
    if not intent_id or intent_id not in orders:
        raise HTTPException(status_code=404, detail="Intent not found")
    reason     = payload.get("reason", "Primary link degraded — automatic reroute")
    order_path = orders[intent_id].get("network_path", "")
    if "Madrid" in order_path or "ESPANIX" in order_path:
        new_path = "Frankfurt, Germany → Marseille-IX → Madrid, Spain (rerouted)"
    elif "Istanbul" in order_path or "TREX" in order_path:
        new_path = "Istanbul, Turkey → Sofia-IX → Frankfurt, Germany (rerouted)"
    elif "London" in order_path or "LINX" in order_path:
        new_path = "London, UK → AMS-IX Amsterdam → Frankfurt, Germany (rerouted)"
    elif "Frankfurt" in order_path or "DE-CIX" in order_path:
        new_path = "London, UK → AMS-IX Amsterdam → Frankfurt, Germany (rerouted)"
    elif "Dubai" in order_path or "DIX" in order_path:
        new_path = "Riyadh, Saudi Arabia → Bahrain IX → Dubai, UAE (rerouted)"
    else:
        new_path = order_path.replace("→", "→ [backup] →", 1) + " (rerouted)"
    new_router_ip = f"10.{uuid.uuid4().int % 256}.{uuid.uuid4().int % 256}.1"
    timestamp     = datetime.now(timezone.utc).isoformat()
    orders[intent_id]["network_path"]     = new_path
    orders[intent_id]["last_path_update"] = timestamp
    if intent_id in llmrouter_store:
        llmrouter_store[intent_id]["router_ip"]        = new_router_ip
        llmrouter_store[intent_id]["last_path_update"] = timestamp
    persist()
    await write_to_neo4j("/api/audit/pathupdate", {
        "intent_id": intent_id, "new_path": new_path,
        "new_router_ip": new_router_ip, "reason": reason, "timestamp": timestamp
    })
    print(f"[TC] Path update for {intent_id}: {new_path}")
    return {
        "intent_id":     intent_id,
        "new_path":      new_path,
        "new_router_ip": new_router_ip,
        "reason":        reason,
        "status":        "PATH_UPDATED"
    }


# ─────────────────────────────────────────────────────────────────────────────
# LLM Router endpoints
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/mock/llmrouter/provision")
async def llmrouter_provision(payload: Dict):
    intent_id         = payload.get("intent_id")
    sovereignty_token = payload.get("compliance_key") or payload.get("sovereigntyToken")
    if not intent_id or not sovereignty_token:
        raise HTTPException(status_code=400, detail="intent_id and compliance_key required")
    if intent_id not in llmrouter_store:
        llmrouter_store[intent_id] = {"intent_id": intent_id}
    llmrouter_store[intent_id]["sovereigntyToken"] = sovereignty_token
    llmrouter_store[intent_id]["status"]           = "PENDING"
    llmrouter_store[intent_id]["provisioned_at"]   = datetime.now(timezone.utc).isoformat()
    persist()
    print(f"[LLM Router] sovereigntyToken stored for {intent_id} — status: PENDING")
    return {"intent_id": intent_id, "status": "PENDING",
            "message": "sovereigntyToken stored. Awaiting Router_IP from OSS."}


@app.patch("/mock/llmrouter/provision/{intent_id}")
async def llmrouter_activate(intent_id: str, payload: Dict):
    if intent_id not in llmrouter_store:
        raise HTTPException(status_code=404, detail=f"No provision record for intent {intent_id}")
    router_ip = payload.get("router_ip")
    if not router_ip:
        raise HTTPException(status_code=400, detail="router_ip required")
    llmrouter_store[intent_id]["router_ip"]    = router_ip
    llmrouter_store[intent_id]["status"]       = "ACTIVE"
    llmrouter_store[intent_id]["activated_at"] = datetime.now(timezone.utc).isoformat()
    persist()
    print(f"[LLM Router] {intent_id} ACTIVE — Router_IP: {router_ip}")
    return {"intent_id": intent_id, "status": "ACTIVE", "router_ip": router_ip,
            "message": "LLM Router binding complete. Ready for inference."}


@app.get("/mock/llmrouter/provision/{intent_id}")
async def llmrouter_get_binding(intent_id: str):
    binding = llmrouter_store.get(intent_id)
    if not binding:
        raise HTTPException(status_code=404, detail=f"No LLM Router binding for intent {intent_id}")
    if binding.get("status") != "ACTIVE":
        raise HTTPException(status_code=403, detail=f"Intent not ACTIVE — status: {binding.get('status')}")
    return binding


@app.post("/mock/llmrouter/infer")
async def llmrouter_infer(payload: Dict):
    intent_id  = payload.get("intent_id")
    user_query = payload.get("user_query", "")
    if not intent_id:
        raise HTTPException(status_code=400, detail="intent_id required")
    binding = llmrouter_store.get(intent_id)
    if not binding:
        return {"authorized": False, "reason": "No LLM Router binding found for this Intent ID"}
    if binding.get("status") != "ACTIVE":
        return {"authorized": False, "reason": f"Service not ACTIVE — status: {binding.get('status')}"}
    if not binding.get("sovereigntyToken"):
        return {"authorized": False, "reason": "Gate 1 failed — no sovereigntyToken on record"}
    if not binding.get("serviceAccessToken"):
        return {"authorized": False, "reason": "Gate 2a failed — no serviceAccessToken on record"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            bal_resp = await client.get(f"http://localhost:8002/mock/bss/balance/{intent_id}")
        bal_data = bal_resp.json()
        if not bal_data.get("authorized"):
            return {"authorized": False, "reason": f"Gate 2b failed — {bal_data.get('reason')}"}
    except Exception as e:
        return {"authorized": False, "reason": f"Gate 2b failed — BSS unreachable: {e}"}

    print(f"[LLM Router] Inference authorized for {intent_id} — query: {user_query[:60]}")
    azure_api_key     = os.environ.get("AZURE_OPENAI_API_KEY", "")
    azure_endpoint    = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
    azure_deployment  = os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o")
    azure_api_version = os.environ.get("OPENAI_API_VERSION", "2024-02-01")
    ai_response       = ""
    tokens_used       = 0

    if azure_api_key and azure_endpoint and user_query != "__validation_probe__":
        try:
            # -- Build enriched context from stored TMF921 + order data --
            order         = orders.get(intent_id, {})
            tmf921        = order.get("tmf921", {})
            params        = tmf921.get("x-modaas-intentParameters", {})
            constraints   = params.get("constraints", {})
            qos           = constraints.get("qos", {})
            llm           = constraints.get("llm", {})
            prices        = constraints.get("tokenPrices", {})
            sust          = constraints.get("sustainability", {})
            valid_for     = tmf921.get("validFor", {})
            applicability = tmf921.get("x-modaas-applicability", {})
            token_bal     = order.get("token_balance", 0)
            token_start   = order.get("token_balance_start", 0)
            token_used    = order.get("tokens_used", 0)
            token_pct     = round((token_bal / token_start) * 100, 1) if token_start > 0 else 0
            agent_count   = next(
                (c["value"] for c in tmf921.get("characteristic", []) if c["name"] == "agent_count"),
                "unknown"
            )

            prompt = (
                f"You are a sovereign AI assistant on the MoDaaS Enterprise Portal "
                f"(TMF Catalyst 2026, Catalyst ID: C26.0.952).\n"
                f"MoDaaS is built by Cognizant. Champions: Colt Technology Services (EU), "
                f"STC (GCC), Telefonica (EU), Turk Telekom (EU), Verizon (US). "
                f"Partners: Cisco (Transport Controller), Etiya (BSS), Ni2 (OSS).\n\n"

                f"=== CURRENT SESSION ===\n"
                f"Intent ID: {intent_id}\n"
                f"BSS Reference: {order.get('intent_ref', 'unknown')}\n"
                f"Organisation: {order.get('org_name', 'unknown')} "
                f"(Customer ID: {order.get('customer_id', 'unknown')})\n"
                f"Service type: {order.get('service_type', 'unknown')}\n"
                f"Service status: {order.get('status', 'unknown')}\n"
                f"Activated at: {order.get('activated_at', 'unknown')}\n"
                f"Agent count: {agent_count}\n\n"

                f"=== NETWORK ===\n"
                f"Current path: {order.get('network_path', 'unknown')}\n"
                f"Entry point (source): {params.get('entryPoint', {}).get('location', 'unknown')}\n"
                f"Target location: {constraints.get('targetLocation', 'unknown')}\n"
                f"Router IP: {binding.get('router_ip', 'unknown')} (assigned by Cisco TC)\n"
                f"Rerouted: {'Yes — path was updated by TC during session' if 'rerouted' in order.get('network_path', '') else 'No — on original path'}\n\n"

                f"=== QoS SLA ===\n"
                f"Latency SLA: < {qos.get('latency', '?')}{qos.get('latencyUnit', 'ms')}\n"
                f"Bandwidth: {qos.get('bandwidth', '?')} {qos.get('bandwidthUnit', 'Gbps')}\n"
                f"Availability: {qos.get('availability', '?')}%\n\n"

                f"=== LLM CONFIGURATION ===\n"
                f"Model: {llm.get('model', 'unknown')}\n"
                f"Time to first token SLA: < {llm.get('timeToFirstToken', '?')}{llm.get('timeToFirstTokenUnit', 'ms')}\n"
                f"Context window: {llm.get('contextWindow', '?')} tokens\n"
                f"Tool support: {llm.get('toolsSupport', 'unknown')}\n"
                f"Confidentiality: {llm.get('confidentiality', 'unknown')}\n"
                f"Token pricing: input ${prices.get('inputMax', '?')}/MTok, "
                f"output ${prices.get('outputMax', '?')}/MTok\n\n"

                f"=== SOVEREIGNTY & COMPLIANCE ===\n"
                f"Sovereignty zone: {applicability.get('sovereigntyZone', 'unknown')}\n"
                f"TMF915 governance: validated by Cognizant NAI Agent "
                f"(source: {order.get('nai_source', 'unknown')})\n"
                f"Sovereignty token: {(binding.get('sovereigntyToken') or '')[:12]}... (secured)\n"
                f"NAI rules enforced: RULE-001 (zone), RULE-002 (green score >= 3), "
                f"RULE-003 (no data training), RULE-004 (latency < 10ms), "
                f"RULE-005 (input <= $3/MTok, output <= $20/MTok)\n\n"

                f"=== SUSTAINABILITY ===\n"
                f"Green score: {sust.get('minGreenScore', '?')}/5\n\n"

                f"=== TOKEN ECONOMY ===\n"
                f"Starting balance: {token_start:,} tokens\n"
                f"Tokens used: {token_used:,}\n"
                f"Remaining balance: {token_bal:,} ({token_pct}%)\n\n"

                f"=== SERVICE PERIOD ===\n"
                f"Start: {valid_for.get('startDateTime', '?')}\n"
                f"End: {valid_for.get('endDateTime', '?')}\n"
                f"Duration: {valid_for.get('duration', '?')}\n\n"

                f"=== PIPELINE ROLES ===\n"
                f"BSS (Etiya): billing, order management, serviceAccessToken\n"
                f"OSS (Ni2): network planning, digital twin simulation\n"
                f"TC (Cisco): transport controller, path assignment, rerouting\n"
                f"NAI (Cognizant): TMF915 governance, sovereigntyToken generation\n"
                f"LLM Router: dual-gate inference validation "
                f"(Gate 1: sovereignty, Gate 2: billing)\n\n"

                f"=== INSTRUCTIONS ===\n"
                f"- Answer concisely in 2-3 sentences — this is a live demo environment.\n"
                f"- Only state facts present in the session details above.\n"
                f"- If a value is 'unknown' or not in context, say the data is not available "
                f"in the current session rather than guessing.\n"
                f"- Never fabricate latency readings, router IPs, or compliance values.\n\n"

                f"Query: {user_query}"
            )

            url = (f"{azure_endpoint.rstrip('/')}/openai/deployments/"
                   f"{azure_deployment}/chat/completions"
                   f"?api-version={azure_api_version}")

            async with httpx.AsyncClient(timeout=30.0) as gc:
                gr = await gc.post(
                    url,
                    headers={
                        "api-key": azure_api_key,
                        "Content-Type": "application/json"
                    },
                    json={
                        "messages": [{"role": "user", "content": prompt}],
                        "max_completion_tokens": 512,
                        "temperature": 0.7
                    }
                )

            gd          = gr.json()
            print(f"[GPT-4o] Raw response keys: {list(gd.keys())}")
            print(f"[GPT-4o] Status code: {gr.status_code}")
            print(f"[GPT-4o] Choices: {gd.get('choices', 'MISSING')}")
            print(f"[GPT-4o] Error: {gd.get('error', 'NONE')}")
            choice      = gd.get("choices", [{}])[0]
            message     = choice.get("message", {})
            ai_response = message.get("content") or message.get("text") or "[No response from GPT-4o]"
            tokens_used = (gd.get("usage", {}).get("total_tokens", 0)
                           or len(user_query.split()) * 2)
            print(f"[GPT-4o] Tokens used: {tokens_used}")
        except Exception as e:
            print(f"[GPT-4o] Error: {e}")
            ai_response = f"[GPT-4o unavailable: {str(e)[:80]}]"
            tokens_used = len(user_query.split()) * 2
    else:
        ai_response = ("[Validation probe acknowledged]"
                       if user_query == "__validation_probe__"
                       else "[AZURE_OPENAI_API_KEY not set]")
        tokens_used = 0

    if tokens_used > 0:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post("http://localhost:8002/mock/bss/deduct",
                    json={"intent_id": intent_id, "tokens_used": tokens_used,
                          "query_hint": user_query[:40]})
        except Exception as e:
            print(f"[BSS deduct] Failed: {e}")

    order        = orders.get(intent_id, {})
    new_balance  = order.get("token_balance", 0)
    start        = order.get("token_balance_start", DEFAULT_BALANCE)
    balance_pct  = round((new_balance / start) * 100, 1) if start > 0 else 0

    return {
        "authorized":             True,
        "intent_id":              intent_id,
        "router_ip":              binding.get("router_ip"),
        "response":               ai_response,
        "tokens_used":            tokens_used,
        "token_balance":          new_balance,
        "token_balance_start":    start,
        "token_balance_pct":      balance_pct,
        "low_balance":            balance_pct < 20.0,
        "last_recharge":          order.get("last_recharge"),
        "sovereignty_token_hint": (binding.get("sovereigntyToken") or "")[:12] + "...",
        "message":                "Inference completed — sovereign and authenticated."
    }


# ─────────────────────────────────────────────────────────────────────────────
# Admin
# ─────────────────────────────────────────────────────────────────────────────
@app.delete("/mock/admin/reset")
async def admin_reset():
    orders.clear()
    llmrouter_store.clear()
    persist()
    for f in STORE_DIR.glob("rules_*.json"):
        try:
            f.unlink()
        except Exception:
            pass
    print("[Admin] Stores cleared — orders.json, llmrouter.json, rules_*.json reset")
    return {"status": "ok", "message": "All orders, LLM Router bindings, and rules files cleared."}


# ─────────────────────────────────────────────────────────────────────────────
# Health check
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {
        "status":             "ok",
        "service":            "MoDaaS Mock Vendors v2.0 (BSS + OSS + TC + LLM Router)",
        "nai_endpoint":       NAI_ENDPOINT,
        "nai_note":           "NAI is Cognizant Neuro SAN agent — not mocked here",
        "active_orders":      len(orders),
        "llmrouter_bindings": len(llmrouter_store),
        "persistence": {
            "orders_file":       str(ORDERS_FILE),
            "llmrouter_file":    str(LLMROUTER_FILE),
            "orders_on_disk":    ORDERS_FILE.exists(),
            "llmrouter_on_disk": LLMROUTER_FILE.exists(),
        },
        "timestamp": datetime.now(timezone.utc).isoformat()
    }