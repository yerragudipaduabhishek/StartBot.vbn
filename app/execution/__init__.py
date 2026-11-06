"""Execution validation layer: strict testnet-only order path, SL/TP
monitoring, recovery and reporting.

SAFETY CONTRACT
---------------
* This package never places production orders. Every order path runs behind a
  host guard that rejects production REST endpoints while TESTNET_MODE is on.
* Live execution additionally requires LIVE_MODE, TESTNET_MODE off,
  REAL_ORDER_CONFIRMATION=true and an explicit per-call confirm.
* All order requests, responses, fills, rejections and errors are logged and
  reconciled so a restart can never double-place an order.
"""