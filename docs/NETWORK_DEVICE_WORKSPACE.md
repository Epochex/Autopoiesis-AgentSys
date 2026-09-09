# Production topology and gateway interface identities

Visual baseline: `83e5c5b`. The user requested restoration of that original
network graph after reviewing the directory and interface-ring alternatives.

## Current UI

`ProductionTopologyPage` renders the original `TopologyCanvas`, including the
all-network layout, subnet mesh, endpoint relationships, traffic portrait,
pan/zoom and incident views. Search also opens the endpoint ego portrait.
Ordinary browsing does not automatically open a baseline risk-assessment card.

Clicking the FortiGate node or an existing interface expands `GatewayInterfaces`
inside the same SVG canvas, anchored at the original gateway and interface columns.
Dashed links encode configuration ownership; they do not animate or imply traffic.
Primary and secondary IPs share an interface node. Selecting an interface highlights
its ownership path and shows aggregate members or VLAN parent. Clicking its subnet
opens the original terminal mesh and traffic portraits. Search includes managed
device names, serials, interfaces and addresses and focuses the matching interface.
Clicking the gateway again restores the original overview. The top-right table
has been removed. Existing external-source links and subnet interactions remain.

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
