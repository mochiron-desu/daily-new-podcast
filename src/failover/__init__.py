"""Railway-to-local failover coordination, state sync, and HTTP server."""

from .coordinator import (
    FailoverOutcome,
    PeerStatus,
    elect_fallback_runner,
    evaluate_failover_need,
    notify_peers_of_primary_failure,
    probe_node,
    push_state_to_nodes,
    run_failover_check,
    sync_state_from_nodes,
)
from .server import create_app

__all__ = [
    "FailoverOutcome",
    "PeerStatus",
    "create_app",
    "elect_fallback_runner",
    "evaluate_failover_need",
    "notify_peers_of_primary_failure",
    "probe_node",
    "push_state_to_nodes",
    "run_failover_check",
    "sync_state_from_nodes",
]
