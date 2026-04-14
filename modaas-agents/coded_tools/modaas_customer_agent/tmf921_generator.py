"""
TMF921Generator — CodedTool
Assembles a TMF921A v1.1.0 compliant Intent JSON document from questionnaire answers.

Standards reference: TMF921A Intent Management API Profile v1.1.0 (31-Mar-2022)
  - Mandatory fields: id, intentExpression, validFor, lastUpdate (Section 5.1)
  - intentHandlingState enum: RECEIVED, COMPLIANT, DEGRADED, FINALIZING (Section 5.1.2)
  - API root pattern: {api_root}/intentManagement/v4/intent/{id} (Section 7)
  - Domain events supported: IntentReceived, IntentAccepted, IntentRejected (Section 6)

MoDaaS extensions (beyond base spec -- prefixed with x-modaas):
  - applicability, intentParameters, characteristic, relatedParty
  - These carry MoDaaS-specific AI Inference parameters not in base TMF921A
"""

import json
import uuid
import hashlib
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List
from neuro_san.interfaces.coded_tool import CodedTool

# -- Constants -----------------------------------------------------------------
API_ROOT        = "https://modaas.cognizant.com/tmf-api/intentManagement/v4"
SCHEMA_LOCATION = "https://tmforum.org/schemas/tmf921/Intent.schema.json"
CATALYST_ID     = "C26.0.952"
PORTAL_VERSION  = "MoDaaS-Phase2"

# TMF921A intentHandlingState enum values (Section 5.1.2)
STATE_RECEIVED   = "RECEIVED"
STATE_COMPLIANT  = "COMPLIANT"
STATE_DEGRADED   = "DEGRADED"
STATE_FINALIZING = "FINALIZING"

# Supported sovereignty zones per TMF915 RULE-001
GCC_ZONE = ["Saudi Arabia", "UAE", "United Arab Emirates", "Kuwait", "Bahrain", "Qatar", "Oman"]
EU_ZONE  = ["Germany", "France", "Netherlands", "Ireland", "Sweden", "Finland",
            "Denmark", "Belgium", "Austria", "Spain", "Italy", "Portugal", "Turkey",
            "Türkiye", "United Kingdom", "UK"]


class TMF921Generator(CodedTool):
    """
    Builds a TMF921A-compliant Intent document from collected service parameters.

    Returns:
        SUCCESS:    { status, intent_id, tmf921_json, message }
        INCOMPLETE: { status, missing_fields, message }
    """

    async def async_invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Any:

        # -- Generate identifiers ----------------------------------------------
        intent_id   = str(uuid.uuid4())
        created_at  = datetime.now(timezone.utc)
        created_str = created_at.isoformat()

        # -- Parse period - ISO 8601 dates -------------------------------------
        start_date_str, end_date_str, duration_iso = self._parse_period(
            args.get("start_date", ""), args.get("duration", ""), created_at
        )

        # -- Determine sovereignty zone ----------------------------------------
        target_location  = args.get("target_location", "")
        sovereignty_zone = self._get_sovereignty_zone(target_location)

        # -- Parse QoS values (value + unit separated) -------------------------
        latency_val,   latency_unit   = self._parse_value_unit(args.get("latency", ""),              default_unit="ms")
        bandwidth_val, bandwidth_unit = self._parse_value_unit(args.get("bandwidth", ""),            default_unit="Gbps")
        ttft_val,      ttft_unit      = self._parse_value_unit(args.get("time_to_first_token", ""),  default_unit="ms")

        # -- Parse green score as integer --------------------------------------
        green_score = self._parse_green_score(args.get("green_score", "3"))

        # -- Parse context window as numeric tokens ----------------------------
        context_window = self._parse_context_window(args.get("context_window", ""))

        # -- Parse token prices as numeric ------------------------------------
        input_price  = self._parse_price(args.get("input_token_price",  "3"))
        output_price = self._parse_price(args.get("output_token_price", "20"))

        # -- Detect currency unit ---------------------------------------------
        price_unit = self._parse_price_unit(
            args.get("input_token_price", "") or args.get("output_token_price", "")
        )

        # -- Build intentExpression (JSON-LD per TMF921A spec) -----------------
        # Spec: "The Intent API will use an embedded JSON-LD to describe
        #        the content of this IntentExpression" (Section 5.1.1)
        # NOTE: stored as dict (not json.dumps string) to avoid double-serialization
        intent_expression = {
            "@context": "https://tmforum.org/intent/context/v1",
            "@type":    "ServiceIntent",
            "expression":     args.get("original_request", ""),
            "expressionType": "natural-language",
            "owner": {
                "@type":       "Organization",
                "id":          args.get("customer_id", ""),
                "name":        args.get("org_name", ""),
                "agentSystem": "MoDaaS-CustomerAgent"
            }
        }

        # -- Build validFor (TimePeriod - mandatory per spec Section 5.1.1) ---
        valid_for = {
            "startDateTime": start_date_str,
            "endDateTime":   end_date_str,
            "duration":      duration_iso
        }

        # -- Assemble full TMF921A document ------------------------------------
        intent = {
            # -- TMF921A mandatory fields (Section 5.1.1) ---------------------
            "@type":           "Intent",
            "@schemaLocation": SCHEMA_LOCATION,
            "id":              intent_id,
            "href":            f"{API_ROOT}/intent/{intent_id}",
            "name": (
                f"AI Connectivity Service - "
                f"{args.get('source_location', 'N/A')} to "
                f"{args.get('target_location', 'N/A')}"
            ),
            "description": (
                f"Enterprise AI Inference Connectivity service request "
                f"via MoDaaS Portal for {args.get('org_name', 'unknown org')}"
            ),

            # -- TMF921A lifecycle fields --------------------------------------
            # intentHandlingState: RECEIVED on creation (Section 5.1.2)
            "intentHandlingState": STATE_RECEIVED,
            "lifecycleStatus":     "NEW",
            "creationDate":        created_str,
            "lastUpdate":          created_str,   # mandatory per spec Section 5.1.1

            # -- TMF921A core fields -------------------------------------------
            "intentExpression": intent_expression,
            "validFor":         valid_for,

            # -- MoDaaS extensions (x-modaas prefix = non-breaking extension) --
            "x-modaas-applicability": {
                "customerId":       args.get("customer_id", ""),
                "agenticProcessId": f"agent-maas-{intent_id[:8]}",
                "targetService":    args.get("service_type", "AI-Inference-Connectivity"),
                "sovereigntyZone":  sovereignty_zone,
                "channel":          "MoDaaS-Portal"
            },

            "x-modaas-intentParameters": {
                "period": {
                    "startDateTime": start_date_str,
                    "endDateTime":   end_date_str,
                    "duration":      duration_iso
                },
                "entryPoint": {
                    "location": args.get("source_location", ""),
                    "region":   self._get_sovereignty_zone(args.get("source_location", ""))
                },
                "constraints": {
                    "targetLocation":  target_location,
                    "sovereigntyZone": sovereignty_zone,
                    "qos": {
                        "latency":          latency_val,
                        "latencyUnit":      latency_unit,
                        "bandwidth":        bandwidth_val,
                        "bandwidthUnit":    bandwidth_unit,
                        "availability":     self._parse_availability(
                                                args.get("availability", "99.95")
                                            ),
                        "availabilityUnit": "%"
                    },
                    "llm": {
                        "model":                args.get("llm_model", ""),
                        "confidentiality":      "no-data-training"
                                                if "training" in str(
                                                    args.get("confidentiality", "")
                                                ).lower()
                                                else args.get("confidentiality", ""),
                        "timeToFirstToken":     ttft_val,
                        "timeToFirstTokenUnit": ttft_unit,
                        "contextWindow":        context_window,
                        "contextWindowUnit":    "tokens",
                        "toolsSupport":         bool(args.get("tool_support", True)),
                    },
                    "tokenPrices": {
                        "inputMax":  input_price,
                        "outputMax": output_price,
                        "unit":      price_unit
                    },
                    "sustainability": {
                        "minGreenScore": green_score,
                        "maxGreenScore": 5,
                        "scale":         "1-5"
                    }
                }
            },

            # -- characteristic array (TMF SID pattern) ------------------------
            "characteristic": [
                {
                    "name":      "service_type",
                    "value":     args.get("service_type", "AI Inference Connectivity"),
                    "valueType": "string"
                },
                {
                    "name":      "agent_count",
                    "value":     self._parse_agent_count(args.get("agent_count", "")),
                    "valueType": "integer"
                },
                {
                    "name":      "bandwidth",
                    "value":     bandwidth_val,
                    "valueType": "string",
                    "unit":      bandwidth_unit
                },
                {
                    "name":      "catalyst_id",
                    "value":     CATALYST_ID,
                    "valueType": "string"
                },
                {
                    "name":      "portal_version",
                    "value":     PORTAL_VERSION,
                    "valueType": "string"
                }
            ],

            # -- relatedParty array --------------------------------------------
            "relatedParty": [
                {
                    "id":            args.get("customer_id", ""),
                    "name":          args.get("org_name", ""),
                    "role":          "Customer",
                    "@referredType": "Organization"
                },
                {
                    "id":            "cognizant-ca-001",
                    "name":          "Cognizant Customer Agent",
                    "role":          "Initiator",
                    "@referredType": "AgenticSystem"
                }
            ],

            # -- note array (TMF standard audit notes) -------------------------
            "note": [
                {
                    "id":     f"note-{intent_id[:8]}",
                    "date":   created_str,
                    "author": "CustomerAgent_CA",
                    "text": (
                        "Intent captured via MoDaaS Enterprise Portal chatbot. "
                        f"TMF915 sovereignty zone: {sovereignty_zone}. "
                        "NAI Governance Agent validation pending."
                    )
                }
            ],

            # -- Audit fields --------------------------------------------------
            "modificationReason": "Initial capture from MoDaaS Customer Agent",
            "updatedBy":          "CustomerAgent_CA"
        }

        # -- Validate required fields -----------------------------------------
        missing = self._validate(intent, args)
        if missing:
            return {
                "status":         "INCOMPLETE",
                "missing_fields": missing,
                "message": (
                    f"TMF921 cannot be generated. "
                    f"Missing required fields: {', '.join(missing)}"
                )
            }

        # -- Generate intent fingerprint (for audit/deduplication) ------------
        fingerprint_input = (
            f"{args.get('customer_id', '')}:"
            f"{args.get('source_location', '')}:"
            f"{args.get('target_location', '')}:"
            f"{start_date_str}:{duration_iso}"
        )
        intent["x-modaas-fingerprint"] = hashlib.sha256(
            fingerprint_input.encode()
        ).hexdigest()[:16].upper()

        return {
            "status":      "SUCCESS",
            "intent_id":   intent_id,
            "tmf921_json": json.dumps(intent, indent=2),
            "message": (
                f"TMF921 Intent document generated successfully. "
                f"Intent ID: {intent_id}"
            )
        }

    # -- Helper methods --------------------------------------------------------

    def _parse_period(self, start_date: str, duration: str, base_time: datetime):
        """
        Convert relative date phrases to ISO 8601.
        Returns (start_str, end_str, duration_iso).
        """
        start_date_lower = start_date.lower().strip()

        relative_map = {
            "immediately":   timedelta(days=0),
            "today":         timedelta(days=0),
            "tomorrow":      timedelta(days=1),
            "one week":      timedelta(weeks=1),
            "1 week":        timedelta(weeks=1),
            "one month":     timedelta(days=30),
            "1 month":       timedelta(days=30),
            "in one month":  timedelta(days=30),
            "in 1 month":    timedelta(days=30),
            "next month":    timedelta(days=30),
            "two weeks":     timedelta(weeks=2),
            "2 weeks":       timedelta(weeks=2),
        }

        start_dt = None
        for phrase, delta in relative_map.items():
            if phrase in start_date_lower:
                start_dt = base_time + delta
                break

        if not start_dt:
            try:
                start_dt = datetime.fromisoformat(start_date)
            except (ValueError, TypeError):
                start_dt = base_time + timedelta(days=30)

        duration_days = self._parse_duration_days(duration)
        end_dt        = start_dt + timedelta(days=duration_days)
        duration_iso  = f"P{duration_days}D"

        return (
            start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            duration_iso
        )

    def _parse_duration_days(self, duration: str) -> int:
        """Extract number of days from duration string."""
        duration_lower = duration.lower().strip()
        duration_map = {
            "1 day": 1,  "one day": 1,
            "2 days": 2, "two days": 2,
            "3 days": 3, "three days": 3,
            "4 days": 4, "four days": 4,
            "5 days": 5, "five days": 5,
            "7 days": 7, "one week": 7,
            "14 days": 14, "two weeks": 14,
            "30 days": 30, "one month": 30,
        }
        for phrase, days in duration_map.items():
            if phrase in duration_lower:
                return days
        import re
        match = re.search(r'\d+', duration)
        return int(match.group()) if match else 1

    def _parse_value_unit(self, value_str: str, default_unit: str = "") -> tuple:
        """
        Split combined value+unit strings into (value, unit).
        e.g. "10ms" -> ("10", "ms"), "1 Gbps" -> ("1", "Gbps")
        """
        import re
        value_str = str(value_str).strip()
        match = re.match(r'^([<>]?\s*[\d.]+)\s*([a-zA-Z/%]+)?', value_str)
        if match:
            val  = match.group(1).replace(" ", "").replace("<", "").replace(">", "")
            unit = match.group(2) if match.group(2) else default_unit
            return (val, unit)
        return (value_str, default_unit)

    def _parse_availability(self, value: str) -> str:
        """Strip % sign if present and return numeric string."""
        return str(value).replace("%", "").strip()

    def _parse_green_score(self, value: Any) -> int:
        """Extract integer green score from various formats."""
        import re
        match = re.search(r'\d+', str(value))
        return int(match.group()) if match else 3

    def _parse_context_window(self, value: str) -> int:
        """Convert context window to integer tokens. e.g. '128k' -> 128000"""
        import re
        value_str = str(value).lower().strip()
        match = re.search(r'([\d.]+)\s*k', value_str)
        if match:
            return int(float(match.group(1)) * 1000)
        match = re.search(r'\d+', value_str)
        return int(match.group()) if match else 128000

    def _parse_price(self, value: Any) -> float:
        """Extract numeric price from string. e.g. '$3/MTok' -> 3.0, '3 euros' -> 3.0"""
        import re
        match = re.search(r'[\d.]+', str(value))
        return float(match.group()) if match else 0.0

    def _parse_price_unit(self, value: Any) -> str:
        """
        Detect currency unit from price string.
        Returns 'EUR/MTok' if euros detected, otherwise 'USD/MTok'.
        Examples: '$3/MTok' -> 'USD/MTok', '3 euros per MTok' -> 'EUR/MTok'
        """
        s = str(value).lower()
        if any(x in s for x in ["eur", "euro"]):
            return "EUR/MTok"
        return "USD/MTok"

    def _parse_agent_count(self, value: Any) -> int:
        """Extract integer agent count."""
        import re
        match = re.search(r'\d+', str(value))
        return int(match.group()) if match else 0

    def _get_sovereignty_zone(self, location: str) -> str:
        """Determine sovereignty zone from location string."""
        location_upper = location.upper()
        for country in GCC_ZONE:
            if country.upper() in location_upper:
                return "GCC"
        for country in EU_ZONE:
            if country.upper() in location_upper:
                return "EU"
        return "UNKNOWN"

    def _validate(self, intent: Dict, args: Dict) -> List[str]:
        """
        Validate all required TMF921A fields and MoDaaS-specific fields.
        Returns list of missing field names (empty = valid).
        """
        missing = []

        # TMF921A mandatory fields (Section 5.1.1)
        if not intent.get("id"):
            missing.append("id")
        if not intent.get("intentExpression"):
            missing.append("intentExpression")
        if not intent.get("validFor", {}).get("startDateTime"):
            missing.append("validFor.startDateTime")
        if not intent.get("lastUpdate"):
            missing.append("lastUpdate")

        # MoDaaS required fields
        checks = [
            ("org_name",        intent["relatedParty"][0]["name"]),
            ("customer_id",     intent["x-modaas-applicability"]["customerId"]),
            ("source_location", intent["x-modaas-intentParameters"]["entryPoint"]["location"]),
            ("target_location", args.get("target_location", "")),
            ("latency",         intent["x-modaas-intentParameters"]["constraints"]["qos"]["latency"]),
            ("bandwidth",       intent["x-modaas-intentParameters"]["constraints"]["qos"]["bandwidth"]),
            ("llm_model",       intent["x-modaas-intentParameters"]["constraints"]["llm"]["model"]),
            ("green_score",     str(intent["x-modaas-intentParameters"]["constraints"]["sustainability"]["minGreenScore"])),
            ("original_request", args.get("original_request", "")),
        ]
        for field_name, value in checks:
            if not str(value).strip():
                missing.append(field_name)

        # Sovereignty zone check (TMF915 RULE-001 pre-validation)
        zone = intent["x-modaas-applicability"].get("sovereigntyZone", "UNKNOWN")
        if zone == "UNKNOWN":
            missing.append("sovereignty_zone_unrecognized")

        return missing