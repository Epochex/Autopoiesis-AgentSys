# Production topology and gateway interface identities

Visual baseline: `83e5c5b`. The user requested restoration of that original
network graph after reviewing the directory and interface-ring alternatives.

## Current UI

`ProductionTopologyPage` renders the original `TopologyCanvas`, including the
all-network layout, subnet mesh, endpoint relationships, traffic portrait,
pan/zoom and incident views. Search also opens the endpoint ego portrait.
Ordinary browsing does not automatically open a baseline risk-assessment card.

`GatewayInterfaces` adds a small, initially collapsed interface-address annotation
at the top right of the existing canvas. Addresses are grouped under the attested
FortiGate serial, with interface names, primary/secondary IPs, roles and aggregate
members. This annotation does not move nodes or replace traffic relationships.

The superseded directory workspace, interface-ring graph and their styles were
removed. They remain recoverable from commits `b713eb4` and `ddf318d`.

## Retained data model

`network_inventory.py` derives managed identity and interface ownership from the
FortiGate configuration. `fortigate_api.py` retains secondary IPs, VDOM, interface
type, parent and aggregate members. The overview exposes these as
`inventory.managedDevices`. Owned gateway addresses are excluded from the ordinary
terminal/public-destination projection.

The existing read-only `/api/rca/interface-traffic` endpoint is retained but is not
used by this compact annotation. It reports recorded session bytes relative to
the requested interface and separates forwarding from appliance traffic. These
counts must not be summed across interfaces. Its configured store covers one
connected FortiGate; duplicate interface names across VDOMs are rejected because
the fact table lacks collector/VDOM dimensions.

Identity grouping is separate from traffic aggregation. This restore does not
introduce an office-wide upload/download total or invent device-to-device links.
