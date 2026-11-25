"""Dashboard: the node at a glance, its sessions and routes with their measurements, and charts of what was measured.

Every number comes from the latest snapshot or from the telemetry recorded out of earlier snapshots. A value the library has
not produced yet reads "no data yet", and an empty chart says so.
"""

from __future__ import annotations

from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel

from transferd_gui.model.fmt import NO_DATA, human_age, human_bytes, human_mbps, human_ms, human_percent, short_hex
from transferd_gui.model.snapshot import NodeSnapshot
from transferd_gui.model.telemetry import RECEIVED_MBPS, RTT, SENT_MBPS
from transferd_gui.ui.charts import LineChart
from transferd_gui.ui.pages.base import Page
from transferd_gui.ui.status import relay_status, route_status, session_status
from transferd_gui.ui.widgets import Card, CopyField, Pill, TableWithEmpty

ROUTE_HEADERS = (
    "Peer",
    "Route",
    "State",
    "Sent",
    "Received",
    "Sent rate",
    "Received rate",
    "RTT",
    "Loss",
    "Cost",
    "Library rate",
)
SESSION_HEADERS = ("Peer", "Name", "State", "Protocol", "Routes up", "Services", "Up for")


class DashboardPage(Page):
    key = "dashboard"
    title = "Dashboard"
    explainer = (
        "What this node is doing right now: its sessions, the routes they use, and what the library measured on them. "
        "Charts fill in as measurements arrive, about once a second."
    )

    def build(self) -> None:
        cards = QGridLayout()
        cards.setSpacing(12)
        self.node_card = Card("Node")
        self.node_name = QLabel()
        self.node_id = CopyField("Node id")
        self.listener = QLabel()
        self.listener.setWordWrap(True)
        self.node_card.body.addWidget(self.node_name)
        self.node_card.body.addWidget(self.node_id)
        self.node_card.body.addWidget(self.listener)

        self.relay_card = Card("Relay")
        self.relay_pill = Pill("not connected")
        self.relay_address = QLabel()
        self.relay_next = QLabel()
        self.relay_detail = QLabel()
        self.relay_detail.setWordWrap(True)
        self.relay_card.body.addWidget(self.relay_pill)
        self.relay_card.body.addWidget(self.relay_address)
        self.relay_card.body.addWidget(self.relay_next)
        self.relay_card.body.addWidget(self.relay_detail)

        self.peers_card = Card("Peers")
        self.peers_count = QLabel()
        self.routes_count = QLabel()
        self.peers_card.body.addWidget(self.peers_count)
        self.peers_card.body.addWidget(self.routes_count)

        self.ghost_card = Card("Ghost key")
        self.ghost_status = QLabel()
        self.ghost_status.setWordWrap(True)
        self.ghost_card.body.addWidget(self.ghost_status)

        for index, card in enumerate((self.node_card, self.relay_card, self.peers_card, self.ghost_card)):
            cards.addWidget(card, index // 2, index % 2)
        self.content.addLayout(cards)

        self.sessions = TableWithEmpty(
            SESSION_HEADERS,
            "No peers yet. Connect to a peer from the Peers screen, or accept an invite. Sessions appear here.",
        )
        sessions_card = Card("Sessions")
        sessions_card.body.addWidget(self.sessions)
        self.content.addWidget(sessions_card)

        self.routes = TableWithEmpty(
            ROUTE_HEADERS,
            "No routes yet. A route is one path to a peer. Routes and their measurements appear once a session exists.",
        )
        routes_card = Card("Routes")
        routes_card.body.addWidget(self.routes)
        self.content.addWidget(routes_card)

        window = self.service.settings.telemetry_window_s
        charts = QHBoxLayout()
        self.rtt_chart = LineChart("Round-trip time", "ms", window)
        self.traffic_chart = LineChart("Traffic", "Mbit/s", window)
        charts.addWidget(self.rtt_chart)
        charts.addWidget(self.traffic_chart)
        self.content.addLayout(charts)

    def apply(self, snapshot: NodeSnapshot) -> None:
        identity = snapshot.identity
        self.node_name.setText(f"{identity.name}" if identity.ready else "Starting the node...")
        self.node_id.set_value(identity.node_id)
        self.listener.setText(snapshot.listener.detail)

        relay = snapshot.relay
        text, tone = relay_status(relay)
        self.relay_pill.set_status(text, tone)
        self.relay_address.setText(relay.address or "no relay address set")
        self.relay_next.setText(
            "next registration: " + ("no data yet" if relay.next_registration_at is None else f"in {human_age(relay.next_registration_at - snapshot.taken_at)}")
        )
        self.relay_detail.setText(relay.detail)

        ready = [s for s in snapshot.sessions if s.state == "ready"]
        self.peers_count.setText(f"{len(ready)} ready of {len(snapshot.sessions)} session(s)")
        self.routes_count.setText(f"{snapshot.routes_up} of {len(snapshot.all_routes)} route(s) up")

        self._fill_sessions(snapshot)
        self._fill_routes(snapshot)
        self._fill_charts(snapshot)

    def _fill_sessions(self, snapshot: NodeSnapshot) -> None:
        rows = []
        for session in snapshot.sessions:
            state, _tone = session_status(session)
            rows.append(
                (
                    session.peer_name,
                    short_hex(session.peer_id, 10, 6),
                    state,
                    str(session.version) if session.version else NO_DATA,
                    f"{session.routes_up} of {len(session.routes)}",
                    ", ".join(session.services) or NO_DATA,
                    human_age(snapshot.taken_at - session.established_at),
                )
            )
        self.sessions.set_rows(rows)

    def _fill_routes(self, snapshot: NodeSnapshot) -> None:
        rows = []
        for session in snapshot.sessions:
            for route in session.routes:
                state, _tone = route_status(route.up, route.closed)
                sent_rate = self.telemetry.latest((route.key, SENT_MBPS))
                received_rate = self.telemetry.latest((route.key, RECEIVED_MBPS))
                rows.append(
                    (
                        session.peer_name,
                        route.kind,
                        state,
                        human_bytes(route.sent_bytes),
                        human_bytes(route.received_bytes),
                        human_mbps(sent_rate) if sent_rate is not None else NO_DATA,
                        human_mbps(received_rate) if received_rate is not None else NO_DATA,
                        human_ms(route.rtt_ms) if route.rtt_ms is not None else NO_DATA,
                        human_percent(route.loss) if route.loss is not None else NO_DATA,
                        human_ms(route.cost_ms) if route.cost_ms is not None else NO_DATA,
                        human_mbps(route.mbps) if route.mbps is not None else NO_DATA,
                    )
                )
        self.routes.set_rows(rows)

    def _fill_charts(self, snapshot: NodeSnapshot) -> None:
        routes = snapshot.all_routes
        rtt_series = []
        traffic_series = []
        for route in routes:
            label = f"{route.kind} {route.key}"
            rtt_series.append((label, self.telemetry.points((route.key, RTT))))
            traffic_series.append((f"{label} sent", self.telemetry.points((route.key, SENT_MBPS))))
            traffic_series.append((f"{label} received", self.telemetry.points((route.key, RECEIVED_MBPS))))
        self.rtt_chart.set_series(rtt_series, now=snapshot.taken_at)
        self.traffic_chart.set_series(traffic_series, now=snapshot.taken_at)

    def on_settings(self, settings) -> None:
        self.rtt_chart.set_window(settings.telemetry_window_s)
        self.traffic_chart.set_window(settings.telemetry_window_s)

    def ghost_summary(self, text: str) -> None:
        """The ghost key's one-line state, set by the main window from the ghost service."""
        self.ghost_status.setText(text)

