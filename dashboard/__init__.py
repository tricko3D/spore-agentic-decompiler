"""SAD dashboard application layer.

Operational data and controls live in spore_dashboard_data/control;
knowledge access, consolidation management and HTTP routing are isolated
behind this package. Frontend code does not access SQLite or subprocesses.
"""
