"""Prometheus instruments for MCP tools and the inference downstream."""

from prometheus_client import Counter, Histogram

TOOL_CALLS = Counter(
    "recsys_recommendation_mcp_tool_calls_total",
    "Recommendation MCP calls by terminal status.",
    ("status",),
)
TOOL_DURATION = Histogram(
    "recsys_recommendation_mcp_tool_duration_seconds",
    "Recommendation MCP tool duration.",
)
DOWNSTREAM_REQUESTS = Counter(
    "recsys_recommendation_mcp_downstream_requests_total",
    "Inference API requests by terminal status.",
    ("status",),
)
DOWNSTREAM_DURATION = Histogram(
    "recsys_recommendation_mcp_downstream_duration_seconds",
    "Inference API request duration.",
)
RETRIES = Counter(
    "recsys_recommendation_mcp_retries_total",
    "Bounded retries issued to the inference API.",
    ("reason",),
)
