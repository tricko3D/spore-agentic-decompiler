"""Backwards-compatible CLI import for the re-architected SAD dashboard.

The old HTML and all operational endpoints remain available at /legacy.
The new React/TypeScript frontend is served when its built assets exist.
"""
from dashboard.server import serve

__all__=["serve"]
