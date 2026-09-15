"""Idempotent terminal garbage collection for Recommendation A/B releases."""

from __future__ import annotations

from copy import deepcopy
import json

from .release import digest


TERMINAL = {"COMPLETED", "ROLLED_BACK"}


def _release_id(value):
    return (value or {}).get("release_id")


class TerminalCleanup:
    """Reconcile session, route and capacity lifecycle after a terminal run.

    The state write containing ``INTENT`` always precedes external mutations.
    Every later operation is idempotent, so a Jenkins restart can execute the
    same cleanup again without deleting manifests or replaying inference.
    """

    def __init__(self, store, driver, clock):
        self.store = store
        self.driver = driver
        self.clock = clock
        self.state, self.etag = store.read()

    def save(self, **fields):
        self.state.update(fields)
        self.etag = self.store.write(self.state, self.etag)

    def _pointer_releases(self):
        # In a verified rollback, pending is the quarantined challenger and is
        # intentionally not protected.  In COMPLETED, champion/pending and
        # previous/baseline are the same two retained release identities.
        return {
            release_id
            for release_id in (
                _release_id(self.state.get("champion")),
                _release_id(self.state.get("previous")),
            )
            if release_id
        }

    def run(self):
        if self.driver.scope != "recommendation":
            raise ValueError("terminal cleanup is currently Recommendation-only")
        if self.state.get("phase") == "ROLLBACK_FAILED":
            raise ValueError("route rollback is unverified; cleanup is blocked")
        if self.state.get("phase") not in TERMINAL:
            return {"status": "SKIPPED", "reason": "state is not terminal"}
        if self.state.get("verified_weight") not in {0, 100}:
            raise ValueError("terminal route weight is not verified")

        inventory = self.driver.recommendation_adapter_inventory()
        experiment_id = self.state.get("experiment_id", "baseline")
        previous_cleanup = deepcopy(self.state.get("cleanup", {}))
        attempt = int(previous_cleanup.get("attempt", 0)) + 1
        intent = {
            "schema_version": 1,
            "status": "INTENT",
            "experiment_id": experiment_id,
            "attempt": attempt,
            "started_at": self.clock(),
            "route_weight": self.state["verified_weight"],
            "adapter_inventory_checksum": digest(inventory),
        }
        self.save(cleanup=intent)

        sessions = self.driver.retire_terminal_sessions(
            self.state.get("disabled", [])
        )
        closed_session_count = sessions.get(
            "closed_total",
            int(previous_cleanup.get("closed_session_count", 0))
            + int(sessions["closed"]),
        )
        protected = self._pointer_releases() | set(
            sessions["protected_release_ids"]
        )
        # Only the Recommendation state catalog/quarantine list authorizes a
        # lifecycle mutation. An inventory-only adapter may belong to legacy
        # workflow history that predates scope labels, so report but retain it.
        known_releases = (
            set(self.state.get("releases", {}))
            | set(self.state.get("disabled", []))
            | self._pointer_releases()
            | set(self.state.get("cleanup_managed_release_ids", []))
            | set(previous_cleanup.get("retired_release_ids", []))
        )
        retired = sorted(known_releases - protected)
        retained_releases = {
            release_id: value
            for release_id, value in self.state.get("releases", {}).items()
            if release_id not in retired
        }
        protected_llms = {
            value["llm_version_id"]
            for release_id, value in self.state.get("releases", {}).items()
            if release_id in protected and value.get("llm_version_id")
        }
        for pointer in (self.state.get("champion"), self.state.get("previous")):
            if pointer and pointer.get("llm_version_id"):
                protected_llms.add(pointer["llm_version_id"])

        plan = {
            **intent,
            "status": "ROUTING",
            "sessions": sessions,
            "closed_session_count": closed_session_count,
            "protected_release_ids": sorted(protected),
            "retired_release_ids": retired,
            "retained_release_ids": sorted(known_releases & protected),
            "untracked_adapter_release_ids": sorted(set(inventory) - known_releases),
        }
        self.save(
            cleanup=plan,
            releases=retained_releases,
            cleanup_managed_release_ids=sorted(known_releases),
        )

        revision = self.driver.route(self.state, self.state["verified_weight"])
        if not self.driver.verify_route(
            self.state, self.state["verified_weight"], revision
        ):
            raise RuntimeError("cleanup route was not acknowledged by every ready gateway")
        plan.update(status="RETIRING", route_revision=revision)
        self.save(cleanup=plan, route_revision=revision)

        # A release without a live adapter has no capacity to reclaim. Keep it
        # in the durable ownership registry but avoid noisy no-op Kubernetes
        # reads on every idempotent reconciliation.
        capacity = self.driver.retire_release_capacity(
            [release_id for release_id in retired if release_id in inventory],
            protected_llms,
        )
        evidence = {
            **plan,
            "status": "CLEANED",
            "completed_at": self.clock(),
            "capacity": capacity,
            "manifests_retained": True,
            "evidence_retained": True,
            "inference_requests": 0,
        }
        self.state.setdefault("events", []).append(
            {
                "at": evidence["completed_at"],
                "phase": self.state["phase"],
                "event": "CLEANED",
                "cleanup_attempt": attempt,
                "retired_release_count": len(retired),
            }
        )
        self.save(cleanup=evidence)
        print(
            json.dumps(
                {
                    "event": "ab.cleanup",
                    "experiment_id": experiment_id,
                    "status": "CLEANED",
                    "retired_release_count": len(retired),
                    "closed_session_count": closed_session_count,
                    "closed_this_run": sessions["closed"],
                    "capacity": capacity,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return deepcopy(evidence)
