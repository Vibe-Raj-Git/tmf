"""
LocationValidator — CodedTool
Validates that source and target locations are within the supported GCC or EU zone.
Implements TMF915 RULE-001: Location must be GCC or EU zone.
"""

from typing import Any, Dict
from neuro_san.interfaces.coded_tool import CodedTool


class LocationValidator(CodedTool):
    """
    Validates GCC/EU zone compliance for source and target locations.
    TMF915 RULE-001: REJECT if location is outside GCC or EU zone.
    """

    GCC_ZONE = [
        "riyadh", "jeddah", "saudi arabia", "ksa",
        "dubai", "abu dhabi", "sharjah", "uae",
        "doha", "qatar",
        "manama", "bahrain",
        "muscat", "oman",
        "kuwait city", "kuwait",
    ]

    EU_ZONE = [
        "frankfurt", "berlin", "munich", "germany",
        "amsterdam", "netherlands",
        "paris", "france",
        "london", "uk", "united kingdom",
        "dublin", "ireland",
        "stockholm", "sweden",
        "madrid", "spain",
        "istanbul", "ankara", "turkey", "türkiye",     # Turk Telecom — EU candidate country, included per Catalyst C26.0.952 champion scope
    ]

    async def async_invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Any:
        source = args.get("source_location", "").lower().strip()
        target = args.get("target_location", "").lower().strip()

        source_ok = self._is_in_zone(source)
        target_ok = self._is_in_zone(target)

        if source_ok and target_ok:
            return {
                "status": "APPROVED",
                "source_location": args.get("source_location"),
                "target_location": args.get("target_location"),
                "zone": "GCC/EU",
                "message": f"Route approved: {args.get('source_location')} to {args.get('target_location')} is within the supported GCC/EU zone."
            }

        rejected = []
        if not source_ok:
            rejected.append(f"source '{args.get('source_location')}'")
        if not target_ok:
            rejected.append(f"target '{args.get('target_location')}'")

        return {
            "status": "REJECTED",
            "source_location": args.get("source_location"),
            "target_location": args.get("target_location"),
            "message": f"TMF915 RULE-001 VIOLATION: {', '.join(rejected)} is outside the supported GCC/EU zone."
        }

    def _is_in_zone(self, location: str) -> bool:
        return any(zone in location for zone in self.GCC_ZONE + self.EU_ZONE)