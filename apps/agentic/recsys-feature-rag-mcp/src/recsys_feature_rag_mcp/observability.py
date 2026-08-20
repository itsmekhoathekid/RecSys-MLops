"""Prometheus metrics for MCP tools and downstream HTTP calls."""

from prometheus_client import Counter, Histogram

TOOL_CALLS = Counter(
    "recsys_mcp_tool_calls_total",
    "MCP tool calls classified by completion status.",
    ("tool", "status"),
)
TOOL_DURATION = Histogram(
    "recsys_mcp_tool_duration_seconds",
    "MCP tool execution duration.",
    ("tool",),
)
DOWNSTREAM_REQUESTS = Counter(
    "recsys_mcp_downstream_requests_total",
    "Downstream API requests classified by HTTP status.",
    ("service", "status"),
)
DOWNSTREAM_DURATION = Histogram(
    "recsys_mcp_downstream_duration_seconds",
    "Downstream API request duration.",
    ("service",),
)
PARTIAL_RESULTS = Counter(
    "recsys_mcp_partial_results_total",
    "Composite context responses containing one failed downstream.",
)
