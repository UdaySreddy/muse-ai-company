"""Error type → recovery strategy mapping for MCP API calls.

All tools using _api_call() automatically inherit these recovery hints.
No per-tool maintenance required.
"""

from __future__ import annotations

# HTTP status code → recovery strategy
ERROR_RECOVERY_MAP: dict[int | str, dict[str, str]] = {
    # ---- Client errors ----
    400: {
        "category": "bad_request",
        "recovery": "Request format error. Check that the parameter names and data types are correct.",
    },
    401: {
        "category": "unauthorized",
        "recovery": "Authentication failed. Please check that valid authentication credentials were provided.",
    },
    403: {
        "category": "forbidden",
        "recovery": "Insufficient permission. This operation requires higher privileges; please contact an "
                    "administrator.",
    },
    404: {
        "category": "resource_not_found",
        "recovery": "Confirm the resource ID is correct. Try querying the available resources with a list tool "
                    "first, then operate again.",
    },
    409: {
        "category": "conflict",
        "recovery": "Resource state conflict. Query the current state first (task_status/team_status), then decide "
                    "the next step.",
    },
    422: {
        "category": "validation_error",
        "recovery": "Parameter validation failed. Check that the required parameters are provided and correctly "
                    "formatted.",
    },
    429: {
        "category": "rate_limited",
        "recovery": "Request rate too high. Wait a moment and retry.",
    },
    # ---- Server errors ----
    500: {
        "category": "server_error",
        "recovery": "Internal server error. Wait 3 seconds and retry once. If it still fails, use os_health_check "
                    "to check the system state.",
    },
    502: {
        "category": "bad_gateway",
        "recovery": "Upstream service unavailable. Use os_health_check to check the system state and retry later.",
    },
    503: {
        "category": "service_unavailable",
        "recovery": "Service temporarily unavailable. Use os_health_check to check the system state and retry after "
                    "the service recovers.",
    },
    # ---- Connection-level errors ----
    "connection_refused": {
        "category": "api_unavailable",
        "recovery": "The API service is not running. The system will start it automatically; please retry shortly.",
    },
    "timeout": {
        "category": "timeout",
        "recovery": "Request timed out. Retry shortly; if the timeouts persist, use os_health_check to diagnose.",
    },
    "unknown": {
        "category": "unknown",
        "recovery": "Unknown error. Use os_health_check to check the system state and view the logs for details.",
    },
}

# Business-level error keywords in response body → error category
# Matched against lower-cased response body text (longest key wins on tie)
BUSINESS_ERROR_MAP: dict[str, str] = {
    "\u4e0d\u5b58\u5728": "resource_not_found",
    "not found": "resource_not_found",
    "does not exist": "resource_not_found",
    "already exists": "conflict",
    "\u5df2\u5b58\u5728": "conflict",
    "\u5df2\u7ed3\u675f": "state_conflict",
    "has ended": "state_conflict",
    "permission": "permission_denied",
    "\u6743\u9650": "permission_denied",
    "invalid": "validation_error",
    "\u65e0\u6548": "validation_error",
}


def get_http_recovery(status_code: int) -> dict[str, str]:
    """Return recovery info for an HTTP status code.

    Falls back to the generic server_error entry for 5xx codes, and
    an empty dict for codes with no mapping.
    """
    if status_code in ERROR_RECOVERY_MAP:
        return ERROR_RECOVERY_MAP[status_code]
    if status_code >= 500:
        return ERROR_RECOVERY_MAP[500]
    return {}


def get_connection_recovery(error_str: str) -> dict[str, str]:
    """Return recovery info for a connection-level error string."""
    lowered = error_str.lower()
    if "connection refused" in lowered or "connect" in lowered:
        return ERROR_RECOVERY_MAP["connection_refused"]
    if "timed out" in lowered or "timeout" in lowered:
        return ERROR_RECOVERY_MAP["timeout"]
    return ERROR_RECOVERY_MAP["unknown"]


def get_business_recovery(response_body: str) -> str:
    """Return error category string if any business keyword found in body.

    Returns empty string when no keyword matches.
    """
    lowered = response_body.lower()
    for keyword, category in BUSINESS_ERROR_MAP.items():
        if keyword.lower() in lowered:
            return category
    return ""
