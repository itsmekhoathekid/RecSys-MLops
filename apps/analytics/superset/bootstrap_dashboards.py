from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any

import requests


API_ROOT = os.getenv("SUPERSET_URL", "http://recsys-analytics-superset:8088").rstrip("/")
DATABASE_NAME = os.getenv("SUPERSET_ANALYTICS_DATABASE_NAME", "RecSysAnalytics")
DASHBOARD_TITLE = "RecSys Business Pulse"
DASHBOARD_SLUG = "recsys-business-pulse"
NAMESPACE = uuid.UUID("b22a4eb0-a149-4cd2-a87f-77912461c7b0")


FUNNEL_DAILY_SQL = """
select
    metric_date,
    sessions,
    users,
    impressions,
    clicks,
    carts,
    purchases,
    cast(revenue as double) as revenue,
    100.0 * ctr as ctr_pct,
    100.0 * click_to_purchase_cvr as click_to_purchase_cvr_pct,
    100.0 * impression_to_purchase_cvr as impression_to_purchase_cvr_pct
from analytics.recsys.mart_recsys_funnel_daily
""".strip()

FUNNEL_STAGES_SQL = """
select metric_date, '1. Impressions' as stage, 1 as stage_order, impressions as stage_value
from analytics.recsys.mart_recsys_funnel_daily
union all
select metric_date, '2. Clicks' as stage, 2 as stage_order, clicks as stage_value
from analytics.recsys.mart_recsys_funnel_daily
union all
select metric_date, '3. Carts' as stage, 3 as stage_order, carts as stage_value
from analytics.recsys.mart_recsys_funnel_daily
union all
select metric_date, '4. Purchases' as stage, 4 as stage_order, purchases as stage_value
from analytics.recsys.mart_recsys_funnel_daily
""".strip()

PRODUCT_PERFORMANCE_SQL = """
select
    metric_date,
    product_id,
    coalesce(category_code, 'unknown') as category_code,
    coalesce(brand_name, 'unknown') as brand_name,
    impressions,
    clicks,
    carts,
    attributed_purchases,
    100.0 * ctr as ctr_pct,
    100.0 * conversion_rate as conversion_rate_pct
from analytics.recsys.mart_product_performance_daily
""".strip()


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    sql: str


DATASETS = (
    DatasetSpec("RecSys Funnel Daily", FUNNEL_DAILY_SQL),
    DatasetSpec("RecSys Funnel Stages", FUNNEL_STAGES_SQL),
    DatasetSpec("RecSys Product Performance", PRODUCT_PERFORMANCE_SQL),
)


def deterministic_uuid(kind: str, name: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{kind}:{name}"))


def simple_metric(column: str, label: str, aggregate: str = "SUM") -> dict[str, Any]:
    return {
        "aggregate": aggregate,
        "column": {
            "column_name": column,
            "description": None,
            "expression": None,
            "filterable": True,
            "groupby": True,
            "is_dttm": column == "metric_date",
            "type": None,
        },
        "datasourceWarning": False,
        "expressionType": "SIMPLE",
        "hasCustomLabel": True,
        "label": label,
        "optionName": f"metric_{column}_{aggregate.lower()}",
        "sqlExpression": None,
    }


def sql_metric(expression: str, label: str) -> dict[str, Any]:
    return {
        "aggregate": None,
        "column": None,
        "datasourceWarning": False,
        "expressionType": "SQL",
        "hasCustomLabel": True,
        "label": label,
        "optionName": f"metric_{uuid.uuid5(NAMESPACE, expression).hex[:12]}",
        "sqlExpression": expression,
    }


def base_params(viz_type: str, dataset_id: int) -> dict[str, Any]:
    return {
        "adhoc_filters": [],
        "datasource": f"{dataset_id}__table",
        "force": False,
        "row_limit": 10000,
        "time_range": "No filter",
        "viz_type": viz_type,
    }


def big_number_params(dataset_id: int, metric: dict[str, Any], subheader: str, fmt: str) -> dict[str, Any]:
    params = base_params("big_number_total", dataset_id)
    params.update(
        {
            "header_font_size": 0.42,
            "metric": metric,
            "subheader": subheader,
            "subheader_font_size": 0.16,
            "y_axis_format": fmt,
        }
    )
    return params


def time_series_params(
    dataset_id: int,
    viz_type: str,
    metrics: list[dict[str, Any]],
    *,
    stacked: bool = False,
    y_axis_format: str = "SMART_NUMBER",
) -> dict[str, Any]:
    params = base_params(viz_type, dataset_id)
    params.update(
        {
            "color_scheme": "supersetColors",
            "granularity_sqla": "metric_date",
            "groupby": [],
            "legendOrientation": "top",
            "legendType": "scroll",
            "metrics": metrics,
            "minorSplitLine": False,
            "only_total": True,
            "opacity": 0.35,
            "order_desc": True,
            "rich_tooltip": True,
            "show_legend": True,
            "stack": "Stack" if stacked else None,
            "tooltipTimeFormat": "%Y-%m-%d",
            "truncateXAxis": False,
            "x_axis": "metric_date",
            "x_axis_label": "Date",
            "x_axis_sort": "metric_date",
            "x_axis_sort_asc": True,
            "y_axis_format": y_axis_format,
            "y_axis_title": "",
        }
    )
    return params


def category_bar_params(dataset_id: int, dimension: str, metric: dict[str, Any]) -> dict[str, Any]:
    params = base_params("echarts_timeseries_bar", dataset_id)
    params.update(
        {
            "color_scheme": "supersetColors",
            "groupby": [],
            "legendOrientation": "top",
            "metrics": [metric],
            "order_desc": True,
            "orientation": "horizontal",
            "rich_tooltip": True,
            "show_legend": False,
            "show_value": True,
            "sort_series_type": "sum",
            "truncateXAxis": False,
            "x_axis": dimension,
            "x_axis_sort": metric,
            "x_axis_sort_asc": False,
            "y_axis_format": "SMART_NUMBER",
        }
    )
    return params


def chart_specs(dataset_ids: dict[str, int]) -> list[dict[str, Any]]:
    daily = dataset_ids["RecSys Funnel Daily"]
    stages = dataset_ids["RecSys Funnel Stages"]
    products = dataset_ids["RecSys Product Performance"]
    return [
        {
            "name": "Revenue",
            "dataset_id": daily,
            "viz_type": "big_number_total",
            "description": "Gross revenue from valid purchases in the selected period.",
            "params": big_number_params(daily, simple_metric("revenue", "Revenue"), "Valid purchase revenue", "$,.2f"),
        },
        {
            "name": "Recommendation Impressions",
            "dataset_id": daily,
            "viz_type": "big_number_total",
            "description": "Total recommendation impressions served.",
            "params": big_number_params(daily, simple_metric("impressions", "Impressions"), "Recommendations served", "SMART_NUMBER"),
        },
        {
            "name": "Click-through Rate",
            "dataset_id": daily,
            "viz_type": "big_number_total",
            "description": "Weighted click-through rate across all impressions.",
            "params": big_number_params(
                daily,
                sql_metric("100.0 * sum(clicks) / nullif(sum(impressions), 0)", "CTR"),
                "Clicks / impressions",
                ".2f",
            ),
        },
        {
            "name": "Attributed Purchases",
            "dataset_id": daily,
            "viz_type": "big_number_total",
            "description": "Purchases attributed to recommendation impressions.",
            "params": big_number_params(daily, simple_metric("purchases", "Purchases"), "Attributed conversions", "SMART_NUMBER"),
        },
        {
            "name": "Recommendation Conversion Funnel",
            "dataset_id": stages,
            "viz_type": "funnel",
            "description": "Impression-to-purchase journey ordered by funnel stage.",
            "params": {
                **base_params("funnel", stages),
                "color_scheme": "supersetColors",
                "groupby": ["stage"],
                "label_type": "key_value_percent",
                "metric": simple_metric("stage_value", "Events"),
                "number_format": "SMART_NUMBER",
                "show_labels": True,
                "sort_by_metric": True,
            },
        },
        {
            "name": "Daily Engagement Trend",
            "dataset_id": daily,
            "viz_type": "echarts_timeseries_line",
            "description": "Daily recommendation impressions and clicks.",
            "params": time_series_params(
                daily,
                "echarts_timeseries_line",
                [simple_metric("impressions", "Impressions"), simple_metric("clicks", "Clicks")],
            ),
        },
        {
            "name": "Daily Conversion Actions",
            "dataset_id": daily,
            "viz_type": "echarts_timeseries_bar",
            "description": "Daily clicks, carts, and purchases.",
            "params": time_series_params(
                daily,
                "echarts_timeseries_bar",
                [
                    simple_metric("clicks", "Clicks"),
                    simple_metric("carts", "Carts"),
                    simple_metric("purchases", "Purchases"),
                ],
                stacked=True,
            ),
        },
        {
            "name": "Daily Revenue Trend",
            "dataset_id": daily,
            "viz_type": "echarts_area",
            "description": "Daily valid purchase revenue.",
            "params": time_series_params(
                daily,
                "echarts_area",
                [simple_metric("revenue", "Revenue")],
                y_axis_format="$,.2f",
            ),
        },
        {
            "name": "Top Categories by Impressions",
            "dataset_id": products,
            "viz_type": "echarts_timeseries_bar",
            "description": "Categories receiving the most recommendation exposure.",
            "params": category_bar_params(products, "category_code", simple_metric("impressions", "Impressions")),
        },
        {
            "name": "Top Brands by Purchases",
            "dataset_id": products,
            "viz_type": "echarts_timeseries_bar",
            "description": "Brands with the most attributed purchases.",
            "params": category_bar_params(products, "brand_name", simple_metric("attributed_purchases", "Purchases")),
        },
        {
            "name": "Category Share of Clicks",
            "dataset_id": products,
            "viz_type": "pie",
            "description": "Click contribution by product category.",
            "params": {
                **base_params("pie", products),
                "color_scheme": "supersetColors",
                "donut": True,
                "emit_filter": True,
                "groupby": ["category_code"],
                "label_line": True,
                "label_type": "key_percent",
                "legendOrientation": "right",
                "metric": simple_metric("clicks", "Clicks"),
                "show_labels": True,
                "show_legend": True,
            },
        },
        {
            "name": "Product Performance Explorer",
            "dataset_id": products,
            "viz_type": "table",
            "description": "Product-level reach and conversion performance for detailed analysis.",
            "params": {
                **base_params("table", products),
                "all_columns": ["product_id", "category_code", "brand_name"],
                "color_pn": True,
                "include_search": True,
                "metrics": [
                    simple_metric("impressions", "Impressions"),
                    simple_metric("clicks", "Clicks"),
                    simple_metric("carts", "Carts"),
                    simple_metric("attributed_purchases", "Purchases"),
                    sql_metric("100.0 * sum(clicks) / nullif(sum(impressions), 0)", "CTR %"),
                    sql_metric("100.0 * sum(attributed_purchases) / nullif(sum(impressions), 0)", "CVR %"),
                ],
                "order_desc": True,
                "page_length": 25,
                "percent_metrics": [],
                "server_pagination": False,
                "table_filter": True,
                "timeseries_limit_metric": simple_metric("impressions", "Impressions"),
            },
        },
    ]


def build_query_context(dataset_id: int, params: dict[str, Any]) -> str:
    metrics = params.get("metrics") or ([params["metric"]] if params.get("metric") else [])
    columns = list(params.get("groupby") or params.get("all_columns") or [])
    x_axis = params.get("x_axis")
    is_timeseries = x_axis == "metric_date"
    if x_axis and not is_timeseries and x_axis not in columns:
        columns.append(x_axis)

    query: dict[str, Any] = {
        "columns": columns,
        "filters": [],
        "granularity": "metric_date" if is_timeseries else None,
        "is_timeseries": is_timeseries,
        "metrics": metrics,
        "order_desc": params.get("order_desc", True),
        "orderby": [],
        "post_processing": [],
        "row_limit": params.get("row_limit", 10000),
        "series_limit": 0,
        "time_range": params.get("time_range", "No filter"),
        "url_params": {},
    }
    return json.dumps(
        {
            "datasource": {"id": dataset_id, "type": "table"},
            "force": False,
            "form_data": params,
            "queries": [query],
            "result_format": "json",
            "result_type": "full",
        }
    )


class SupersetClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.headers: dict[str, str] = {}

    def wait_until_ready(self) -> None:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                if self.session.get(f"{API_ROOT}/health", timeout=5).ok:
                    return
            except requests.RequestException:
                pass
            time.sleep(3)
        raise RuntimeError("Superset did not become healthy within 180 seconds")

    def login(self) -> None:
        payload = {
            "password": os.environ["SUPERSET_ADMIN_PASSWORD"],
            "provider": "db",
            "refresh": True,
            "username": os.environ["SUPERSET_ADMIN_USERNAME"],
        }
        response = self.session.post(f"{API_ROOT}/api/v1/security/login", json=payload, timeout=30)
        response.raise_for_status()
        self.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
        csrf = self.get("/api/v1/security/csrf_token/").get("result")
        if csrf:
            self.headers["X-CSRFToken"] = csrf

    def request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        response = self.session.request(
            method,
            f"{API_ROOT}{path}",
            headers=self.headers,
            timeout=120,
            **kwargs,
        )
        if not response.ok:
            raise RuntimeError(f"Superset API {method} {path} failed ({response.status_code}): {response.text[:1000]}")
        return response

    def get(self, path: str) -> dict[str, Any]:
        return self.request("GET", path).json()

    def list_all(self, resource: str) -> list[dict[str, Any]]:
        return self.get(f"/api/v1/{resource}/?q=(page:0,page_size:500)").get("result", [])

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", path, json=payload).json()

    def put(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("PUT", path, json=payload).json()


def upsert_dashboard(client: SupersetClient) -> int:
    existing = next((item for item in client.list_all("dashboard") if item.get("slug") == DASHBOARD_SLUG), None)
    payload = {
        "css": DASHBOARD_CSS,
        "dashboard_title": DASHBOARD_TITLE,
        "json_metadata": json.dumps(
            {
                "color_scheme": "supersetColors",
                "cross_filters_enabled": True,
                "default_filters": "{}",
                "expanded_slices": {},
                "native_filter_configuration": [],
                "refresh_frequency": 0,
                "timed_refresh_immune_slices": [],
            }
        ),
        "published": True,
        "slug": DASHBOARD_SLUG,
        "uuid": deterministic_uuid("dashboard", DASHBOARD_SLUG),
    }
    if existing:
        client.put(f"/api/v1/dashboard/{existing['id']}", payload)
        return int(existing["id"])
    return int(client.post("/api/v1/dashboard/", payload)["id"])


def upsert_datasets(client: SupersetClient, database_id: int) -> dict[str, int]:
    existing = {item["table_name"]: item for item in client.list_all("dataset")}
    result: dict[str, int] = {}
    for spec in DATASETS:
        payload = {
            "catalog": "analytics",
            "database": database_id,
            "schema": "recsys",
            "sql": spec.sql,
            "table_name": spec.name,
            "uuid": deterministic_uuid("dataset", spec.name),
        }
        current = existing.get(spec.name)
        if current:
            dataset_id = int(current["id"])
            update_payload = {**payload, "database_id": payload["database"]}
            update_payload.pop("database")
            client.put(f"/api/v1/dataset/{dataset_id}", update_payload)
        else:
            dataset_id = int(client.post("/api/v1/dataset/", payload)["id"])
        client.request("PUT", f"/api/v1/dataset/{dataset_id}/refresh")
        result[spec.name] = dataset_id
    return result


def upsert_charts(
    client: SupersetClient,
    dashboard_id: int,
    dataset_ids: dict[str, int],
) -> list[dict[str, Any]]:
    existing = {item["slice_name"]: item for item in client.list_all("chart")}
    result: list[dict[str, Any]] = []
    for spec in chart_specs(dataset_ids):
        payload = {
            "dashboards": [dashboard_id],
            "datasource_id": spec["dataset_id"],
            "datasource_type": "table",
            "description": spec["description"],
            "params": json.dumps(spec["params"]),
            "query_context": build_query_context(spec["dataset_id"], spec["params"]),
            "query_context_generation": True,
            "slice_name": spec["name"],
            "uuid": deterministic_uuid("chart", spec["name"]),
            "viz_type": spec["viz_type"],
        }
        current = existing.get(spec["name"])
        if current:
            chart_id = int(current["id"])
            client.put(f"/api/v1/chart/{chart_id}", payload)
        else:
            chart_id = int(client.post("/api/v1/chart/", payload)["id"])
        result.append({"id": chart_id, "name": spec["name"], "uuid": payload["uuid"]})
    return result


def dashboard_position(charts: list[dict[str, Any]]) -> str:
    widths = [3, 3, 3, 3, 4, 8, 6, 6, 6, 6, 4, 8]
    heights = [12, 12, 12, 12, 24, 24, 24, 24, 24, 24, 24, 28]
    rows = [[0, 1, 2, 3], [4, 5], [6, 7], [8, 9], [10, 11]]
    positions: dict[str, Any] = {
        "DASHBOARD_VERSION_KEY": "v2",
        "ROOT_ID": {"children": ["GRID_ID"], "id": "ROOT_ID", "type": "ROOT"},
        "GRID_ID": {"children": [], "id": "GRID_ID", "parents": ["ROOT_ID"], "type": "GRID"},
    }
    for row_number, indexes in enumerate(rows, start=1):
        row_id = f"ROW-{row_number}"
        positions["GRID_ID"]["children"].append(row_id)
        positions[row_id] = {
            "children": [],
            "id": row_id,
            "meta": {"background": "BACKGROUND_TRANSPARENT"},
            "parents": ["ROOT_ID", "GRID_ID"],
            "type": "ROW",
        }
        for index in indexes:
            chart = charts[index]
            chart_key = f"CHART-{chart['id']}"
            positions[row_id]["children"].append(chart_key)
            positions[chart_key] = {
                "children": [],
                "id": chart_key,
                "meta": {
                    "chartId": chart["id"],
                    "height": heights[index],
                    "sliceName": chart["name"],
                    "uuid": chart["uuid"],
                    "width": widths[index],
                },
                "parents": ["ROOT_ID", "GRID_ID", row_id],
                "type": "CHART",
            }
    return json.dumps(positions)


DASHBOARD_CSS = """
.dashboard-content { background: linear-gradient(180deg, #0b1220 0%, #111827 100%); }
.dashboard-component-chart-holder {
  border: 1px solid rgba(148, 163, 184, 0.18);
  border-radius: 14px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.20);
  overflow: hidden;
}
.dashboard-component-chart-holder:hover {
  border-color: rgba(56, 189, 248, 0.50);
  box-shadow: 0 12px 32px rgba(14, 165, 233, 0.12);
}
.dashboard-component-header { letter-spacing: -0.02em; }
""".strip()


def main() -> None:
    client = SupersetClient()
    client.wait_until_ready()
    client.login()

    databases = client.list_all("database")
    database = next((item for item in databases if item.get("database_name") == DATABASE_NAME), None)
    if not database:
        raise RuntimeError(f"Superset database {DATABASE_NAME!r} does not exist")

    dashboard_id = upsert_dashboard(client)
    dataset_ids = upsert_datasets(client, int(database["id"]))
    charts = upsert_charts(client, dashboard_id, dataset_ids)
    client.put(
        f"/api/v1/dashboard/{dashboard_id}",
        {
            "css": DASHBOARD_CSS,
            "dashboard_title": DASHBOARD_TITLE,
            "json_metadata": json.dumps(
                {
                    "color_scheme": "supersetColors",
                    "cross_filters_enabled": True,
                    "default_filters": "{}",
                    "expanded_slices": {},
                    "native_filter_configuration": [],
                    "refresh_frequency": 0,
                    "timed_refresh_immune_slices": [],
                }
            ),
            "position_json": dashboard_position(charts),
            "published": True,
            "slug": DASHBOARD_SLUG,
        },
    )
    print(f"Bootstrapped dashboard {DASHBOARD_SLUG}: {len(dataset_ids)} datasets, {len(charts)} charts")


if __name__ == "__main__":
    main()
