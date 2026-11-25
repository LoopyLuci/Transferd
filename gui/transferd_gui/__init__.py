"""transferd_gui: a desktop dashboard and toolbox for the Transferd Python library.

Layers, from the bottom up:

- `model`: pure Python with no Qt and no library imports. Settings, the event log, telemetry ring buffers and the
  immutable snapshots the views read. Fully unit tested.
- `backend`: the adapter that drives `python/transferd` on an asyncio loop in a worker thread, and turns library objects
  into plain snapshots. Nothing in here touches a widget.
- `ui`: the Qt views. They only read snapshots and call the controller.
"""

__version__ = "0.1.0"
