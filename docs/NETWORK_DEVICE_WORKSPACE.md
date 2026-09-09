# Production network device workspace

Pre-refactor checkpoint: `83e5c5b`.

## Ownership and navigation

`ProductionTopologyPage` owns polling, current source status, incident navigation,
and the separately routed 3D view. The production UI retains the interactive mesh:
`ManagedNetworkGraph` displays serial-owned interfaces and attached terminals in
the all-network view. Clicking a LAN interface opens the original `TopologyCanvas`
subnet mesh; clicking an endpoint focuses its original relationship and traffic
portrait. `NetworkWorkspace` is used only inside a closable node-details drawer.
Ordinary browsing does not open baseline risk-assessment cards.

Component tree:

```text
ProductionTopologyPage
  LiveAlerts
  ManagedNetworkGraph (all-network view)
    Serial-owned device node
    Interface nodes with primary and secondary IP labels
    MAC-associated terminal nodes and attachment edges
  TopologyCanvas (expanded subnet / endpoint)
    Original endpoint glyphs, mesh, pan/zoom and flow portrait
  Node-details drawer (explicit selection only)
    NetworkWorkspace
      InterfaceTable / TrafficPanel / AddressPortrait
```

Files:

- `frontend/gateway/app/network_inventory.py`: serial-based managed identity,
  interface ownership and interface flow aggregation.
- `domains/network_rca/fortigate_api.py`: primary/secondary addresses, VDOM,
  interface type, aggregate membership and parent interface extraction.
- `frontend/gateway/app/situational_awareness.py`: managed inventory in the
  overview; gateway-owned addresses excluded from terminal inventory and public
  destination projection.
- `frontend/src/components/network-model.ts`: ownership lookup and terminal
  grouping by observed MAC. Hostnames alone never merge devices. Addresses without
  MAC evidence remain explicitly unconfirmed identities.
- `frontend/src/components/NetworkWorkspace.tsx` and `network-workspace.css`:
  node details, interface selection and responsive detail layout.
- `frontend/src/components/ManagedNetworkGraph.tsx` and `managed-network-graph.css`:
  interactive all-network mesh and the drawer integration. Solid edges mean
  ownership; dashed edges mean attachment inferred from logs or configured subnet.
  Multi-address terminal identities have one glyph and multiple attachment edges.
- `frontend/src/components/TopologyCanvas.tsx`: retains the original subnet and
  terminal relationship UX; external search selections now open the ego portrait.
- `frontend/src/components/NetworkTrafficViews.tsx` and `network-read.ts`:
  interface and address portraits; cancellable reads, timeout/retry handling,
  and non-overlapping thirty-second refreshes.

## Traffic measurement

`GET /api/rca/interface-traffic` accepts a device ID and interface ID that must
exist in the current inventory, plus a 1-to-7-day window. It groups all matching
records without a Top-N cutoff, ordered by last observation.

For `srcintf`, `sentbyte` enters the interface (RX) and `rcvdbyte` leaves it (TX).
For `dstintf`, the mapping is reversed. Hairpin traffic crossing the same interface
twice contributes to both legs. Forwarded and appliance-local bytes are separate.
These are recorded session bytes, not hardware interface counters. Cross-interface
addition would double-count forwarded sessions.

The configured fact store currently belongs to one connected FortiGate and lacks
collector serial/VDOM columns. The endpoint rejects multiple managed devices and
ambiguous interface names across VDOMs. Additional collectors require these
dimensions to be retained in the traffic store before their totals can be served.

An interface with no recorded traffic shows one empty-state message without
zero-byte metric tiles. Empty WAN forwarding evidence is shown as unavailable office-total coverage.
FortiView current objects have no per-destination timestamps in the present adapter;
the UI labels that boundary. Historical objects use the recorded latest timestamps.
The existing history endpoint can widen an empty window to retained history; the
page explicitly displays the widened interval and actual observed coverage.

## Verification

Tests cover primary/secondary IP ownership, unaddressed interfaces, MAC grouping,
no hostname-only merging, gateway exclusion, RX/TX direction including hairpin,
and rejection of ambiguous VDOM interface names. Browser checks cover device → WAN,
device → LAN → terminal, live/history switching, gateway-address navigation,
and compact viewport layout.
