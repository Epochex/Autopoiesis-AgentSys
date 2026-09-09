# Internal console address

Canonical entry: http://autopoiesis.internal

Nginx serves the console on port 80 using `nginx/autopoiesis-internal.conf`.
API requests proxy to the existing loopback gateway at `127.0.0.1:8026`.
`nginx/netops-ops-console.conf` redirects old port-2026 bookmarks to the canonical
host with HTTP 308. Deploy both configurations. Other Nginx virtual hosts remain
independent.

## Internal DNS

The FortiGate shadow zone `autopoiesis-internal` serves domain `internal`, with
A record `autopoiesis` pointing to `192.168.1.27`, TTL 300 seconds.
Recursive DNS is enabled on the three business network interfaces:

| DHCP ID | Interface | Resolver advertised by DHCP |
| --- | --- | --- |
| 7 | 64网段showroo | 192.168.48.1 |
| 8 | LACP | 192.168.16.1 |
| 9 | fortilink | 192.168.1.1 |

Their DHCP `dns-service` is `local`. Existing external forwarders are retained.
Management/AP-management and the inactive port6 DHCP scopes were left unchanged.
Existing DHCP leases last seven days; clients must renew their lease or reconnect
to receive the new resolver. Clients with manually configured public DNS or a
browser DNS override need the internal resolver for this domain.

The console server has a persistent systemd-resolved routing domain in
`/etc/systemd/resolved.conf.d/autopoiesis-internal.conf`: only
`~autopoiesis.internal` uses `192.168.1.1`; its existing external resolution remains.

## Validation and rollback

Validated UDP/TCP resolution through the directly attached FortiGate LAN,
external DNS forwarding, canonical HTTP/API responses and the legacy redirect.
Cross-interface DNS queries from the server to other VLAN gateway addresses are
not reachable; the interface settings were read back from the gateway API.

Pre-change DNS/DHCP settings and the original Nginx entry are stored under
`/data/autopoiesis-production/deploy-backups/` as
`internal-domain-20260908.json` and `netops-ops-console.before-domain.conf`.
Rollback restores the three DHCP DNS modes to their recorded values, removes
only the added resolver-interface entries and zone, restores the old Nginx
entry, removes the added domain virtual host and resolved drop-in, then validates
Nginx before reloading it and restarts systemd-resolved.
