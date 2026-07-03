from __future__ import annotations

import os

from locust import HttpUser, between, task


def _int_list(name: str, default: str) -> list[int]:
    return [int(item.strip()) for item in os.getenv(name, default).split(",") if item.strip()]


def _candidates() -> list[int]:
    if os.getenv("RECSYS_CANDIDATE_COUNT"):
        count = int(os.environ["RECSYS_CANDIDATE_COUNT"])
        start = int(os.getenv("RECSYS_CANDIDATE_START", "1"))
        return list(range(start, start + count))
    return _int_list("RECSYS_CANDIDATES", "456,379,287,194,157")


TARGET = os.getenv("RECSYS_LOAD_TARGET", "api").lower()
HOST_HEADER = os.getenv("RECSYS_HOST_HEADER")
USER_ID = int(os.getenv("RECSYS_USER_ID", "50"))
CANDIDATES = _candidates()
TOP_K = int(os.getenv("RECSYS_TOP_K", "3"))


class RecsysServingUser(HttpUser):
    wait_time = between(0.01, 0.05)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if HOST_HEADER:
            headers["Host"] = HOST_HEADER
        return headers

    @task
    def infer(self) -> None:
        if TARGET == "triton":
            self._triton_infer()
        elif TARGET == "feature":
            self._online_features()
        else:
            self._api_recommendations()

    def _api_recommendations(self) -> None:
        payload = {
            "user_id": USER_ID,
            "candidate_item_ids": CANDIDATES,
            "top_k": TOP_K,
        }
        with self.client.post(
            "/recommendations",
            json=payload,
            headers=self._headers(),
            name="api:/recommendations",
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"status={response.status_code} body={response.text[:300]}")
                return
            items = response.json().get("items", [])
            if not items:
                response.failure("empty recommendation items")

    def _online_features(self) -> None:
        payload = {
            "user_id": USER_ID,
            "candidate_item_ids": CANDIDATES,
            "top_k": TOP_K,
        }
        with self.client.post(
            "/online-features",
            json=payload,
            headers=self._headers(),
            name="feature:/online-features",
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"status={response.status_code} body={response.text[:300]}")
                return
            body = response.json()
            if not body.get("user_sequence") or not body.get("item_features"):
                response.failure("empty online feature payload")

    def _triton_infer(self) -> None:
        n_candidates = len(CANDIDATES)
        payload = {
            "inputs": [
                {"name": "candidate_item_id", "shape": [n_candidates], "datatype": "INT64", "data": CANDIDATES},
                {"name": "candidate_category", "shape": [n_candidates], "datatype": "INT64", "data": [(item % 20) + 1 for item in CANDIDATES]},
                {"name": "candidate_brand", "shape": [n_candidates], "datatype": "INT64", "data": [(item % 80) + 1 for item in CANDIDATES]},
                {"name": "candidate_price_bucket", "shape": [n_candidates], "datatype": "INT64", "data": [(item % 4) + 1 for item in CANDIDATES]},
                {"name": "hist_item_id", "shape": [4], "datatype": "INT64", "data": [398, 3, 215, 415]},
                {"name": "hist_event_type", "shape": [4], "datatype": "INT64", "data": [1, 1, 1, 1]},
                {"name": "hist_category", "shape": [4], "datatype": "INT64", "data": [15, 7, 1, 12]},
                {"name": "hist_brand", "shape": [4], "datatype": "INT64", "data": [79, 18, 46, 27]},
                {"name": "hist_price_bucket", "shape": [4], "datatype": "INT64", "data": [1, 1, 1, 2]},
                {"name": "hist_time", "shape": [4], "datatype": "INT64", "data": [0, 0, 0, 0]},
            ],
            "outputs": [
                {"name": "candidate_item_id_out"},
                {"name": "score"},
            ],
        }
        with self.client.post(
            "/v2/models/bst_ensemble/infer",
            json=payload,
            headers=self._headers(),
            name="triton:/v2/models/bst_ensemble/infer",
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"status={response.status_code} body={response.text[:300]}")
                return
            outputs = response.json().get("outputs", [])
            if not outputs:
                response.failure("empty Triton outputs")
