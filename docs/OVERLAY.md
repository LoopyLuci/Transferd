# The Transferd Overlay

The overlay lets an application on one paired device reach a service on another, without:

- an open firewall port;
- a router port-forward;
- a VPN.

It works across a LAN, behind NAT or CGNAT, on IPv4 or IPv6.

Every node keeps only **outbound** connections:

- to a relay (any conforming blind relay; anyone can host one);
- to a peer directly, when there is a path.

Traffic is end-to-end encrypted with Transferd’s hybrid post-quantum identity. The relay sees only ciphertext.

The wire format is specified in [PROTOCOL.md](PROTOCOL.md).

## Pairing and Services

- An invite is signed, carries the node’s identity and how to reach it, and expires.
- Accepting one pairs both ways. The accepting node presents the invite’s one-time nonce in its first hello, and the inviting node then knows the pairing is mutual.
- A node is paired only through an invite. Merely opening a session does not count.
- Trust on first use: a node that later presents a different key for the same id is refused.
- Nothing is exposed until explicitly configured. Each service has an access list (node ids and/or `*paired`).
- An open from anyone else is refused before it reaches the local socket.
- Each service is rate-limited per node.

## How a Path is Found

1. **Direct.** Addresses a peer published are dialed first, each with a short timeout.
2. **Relay.** Both nodes register an identity-derived token on the same relay. The relay forwards opaque datagrams between the mappings their own outbound registrations created.
3. Once a session is up on one kind of path, the node adds the other kind beside it. The session then has a second route to fail over to, and the cheaper one carries the traffic.
4. **Hole punch.** When the dialing node holds a session over the relay (and the peer is paired and speaks protocol version 3 or later), it asks the peer for a direct UDP path. The relay stays; the punch only adds a route.

Details of the hole-punch exchange are in [PROTOCOL.md](PROTOCOL.md) §8.

## Inside a Session

- **One session per peer, many routes.** A new link to a peer joins its live session as another route.
- **Probing.** Every route is probed once a second for RTT, loss and throughput. A route is down after three missed probes in a row, and up again as soon as an authenticated frame arrives on it.
- **Streams.** Streams are multiplexed, flow-controlled with a per-stream window, and sequenced. Acknowledgements are cumulative. Unacknowledged data is resent on another route after the route’s retransmission timeout, so a route dying mid-stream does not break the application stream.
- **Control frames.** Hello, open, accept, window and close carry receipts and are resent until one comes back. Pings and route reports are best effort.
- **Congestion control.** Each route maintains its own state (SRTT, RTTVAR, RTO, congestion window) in the style of NewReno / RFC 6298. Data is paced. A gap reported by SACK counts as a loss.

## Mesh DNS

One node’s names can be answered by a paired node’s resolver over the overlay. The overlay simply carries DNS queries and responses; the remote resolver decides the answers. Failover, query-id rewriting, and counters are implementation concerns.

## Compatibility

The overlay is defined by the wire protocol. Any implementation that correctly speaks the frames in [PROTOCOL.md](PROTOCOL.md) and honors the pairing and access-list rules is a conforming overlay node.
