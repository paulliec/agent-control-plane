"""Streamlit console.

Presentation only. Every query, filter and derived number comes from
`services/dashboard.py` and `services/analytics.py`; nothing in here talks to
the repository, classifies a domain, or decides what a policy means.

Entry point: `streamlit run src/agent_control_plane/dashboard/app.py`
"""

from __future__ import annotations
