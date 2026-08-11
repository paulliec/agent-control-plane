"""Agent Control Plane Lite — observability and governance for AI agents.

Layering (imports only ever point downward):

    dashboard / cli
        -> services (ingestion, policy, analytics)
            -> repositories (Protocol) + adapters (Protocol)
                -> models (vendor-neutral)
"""

from agentops.bootstrap import ControlPlane, build_control_plane

__version__ = "0.1.0"

__all__ = ["ControlPlane", "__version__", "build_control_plane"]
