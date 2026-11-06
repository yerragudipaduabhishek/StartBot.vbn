"""Read-only analysis layer for the dashboard.

Consumes market data (Delta public REST) + the validated strategy engine and
produces decisions, dynamic-leverage recommendations, trade plans and an
audit timeline. Never places orders and never writes to the database.
"""