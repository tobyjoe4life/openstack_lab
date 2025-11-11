#!/usr/bin/env python3
"""
List OpenStack Neutron ports for the current project with rich, diagnostic columns.

Auth: uses environment variables (OS_AUTH_URL, OS_USERNAME, OS_PASSWORD, OS_PROJECT_NAME/ID, etc.)
Requires: pip install openstacksdk

Examples:
    python list_ports.py
    python list_ports.py --wide
    python list_ports.py --json
    python list_ports.py --csv ports.csv
    python list_ports.py --columns id,bound_to,ips,fips,status,secgroups,device_owner
"""

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

try:
    import openstack
except ImportError:
    print("ERROR: openstacksdk is not installed. Try: pip install openstacksdk", file=sys.stderr)
    sys.exit(2)


def parse_isotime(s: str):
    if not s:
        return None
    try:
        # Handle trailing Z
        if s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        return datetime.fromisoformat(s)
    except Exception:
        return None


def human_age(ts: str):
    dt = parse_isotime(ts)
    if not dt:
        return ""
    now = datetime.now(timezone.utc)
    delta = now - dt
    total = int(delta.total_seconds())
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def get_binding_attr(port, key: str):
    """
    Safely retrieve binding attributes that may appear as 'binding:host_id' or 'binding_host_id'.
    """
    alt = key.replace(":", "_")
    val = getattr(port, alt, None)
    if val is not None:
        return val
    try:
        d = port.to_dict(computed=False)
        return d.get(key, d.get(alt))
    except Exception:
        return None


class NameResolver:
    """
    Caches resources and returns a name only if it is present and unambiguous;
    otherwise returns the UUID.
    """
    def __init__(self, conn, project_id: str):
        self.conn = conn
        self.project_id = project_id

        # Primary caches
        self._id = defaultdict(dict)        # type -> {id: obj}
        self._name_to_ids = defaultdict(lambda: defaultdict(set))  # type -> {name: {ids}}

        # Load resources visible to this project
        self._load("network", conn.network.networks())
        self._load("subnet", conn.network.subnets())
        self._load("router", conn.network.routers())
        self._load("security_group", conn.network.security_groups(project_id=project_id))
        self._have_qos = True
        try:
            self._load("qos_policy", conn.network.qos_policies())
        except Exception:
            # QoS extension may be disabled; don't fail hard
            self._have_qos = False
        # Servers are scoped to current project by default
        self._load("server", conn.compute.servers(all_projects=False))

        # Optional (may not be present in all clouds)
        self._have_lb = True
        try:
            self._load("loadbalancer", conn.load_balancer.load_balancers())
        except Exception:
            self._have_lb = False

        # Trunks (optional)
        self.trunks = []
        self.trunk_by_parent_port = {}
        self.trunk_by_id = {}
        self.subport_to_trunk = {}
        try:
            self.trunks = list(conn.network.trunks())
            for t in self.trunks:
                self._index_obj("trunk", t)
                self.trunk_by_id[t.id] = t
                if getattr(t, "port_id", None):
                    self.trunk_by_parent_port[t.port_id] = t
                for sp in getattr(t, "sub_ports", []) or []:
                    pid = sp.get("port_id")
                    if pid:
                        self.subport_to_trunk[pid] = {"trunk": t, "sp": sp}
        except Exception:
            pass  # Trunk extension not enabled

        # Floating IPs -> map by attached port
        self.fips_by_port = defaultdict(list)
        try:
            # Neutron floating IPs
            for ip in conn.network.ips():
                if getattr(ip, "port_id", None):
                    self.fips_by_port[ip.port_id].append(ip)
        except Exception:
            # Older clouds might use compute FIPs; not covered here
            pass

    def _load(self, typ: str, iterable):
        for obj in iterable:
            self._index_obj(typ, obj)

    def _index_obj(self, typ: str, obj):
        oid = getattr(obj, "id", None)
        if not oid:
            return
        self._id[typ][oid] = obj
        name = getattr(obj, "name", None) or getattr(obj, "display_name", None)
        if name:
            self._name_to_ids[typ][name].add(oid)

    def try_name(self, typ: str, oid: str):
        if not oid:
            return None
        obj = self._id[typ].get(oid)
        if not obj:
            return None
        name = getattr(obj, "name", None) or getattr(obj, "display_name", None)
        if not name:
            return None
        ids = self._name_to_ids[typ].get(name, set())
        return name if len(ids) == 1 else None

    def name_or_id(self, typ: str, oid: str):
        if not oid:
            return ""
        nm = self.try_name(typ, oid)
        return nm if nm else oid


def build_rows(conn, resolver: NameResolver, ports):
    rows = []
    for p in ports:
        # Fixed IPs: ip + subnet name (network)
        fixed_ip_strs = []
        for fip in getattr(p, "fixed_ips", []) or []:
            ip = fip.get("ip_address", "")
            subnet_id = fip.get("subnet_id")
            subnet_name = resolver.name_or_id("subnet", subnet_id)
            net_name = ""
            subnet_obj = resolver._id["subnet"].get(subnet_id)
            if subnet_obj:
                net_name = resolver.name_or_id("network", getattr(subnet_obj, "network_id", ""))
            fixed_ip_strs.append(f"ip_address='{ip}', subnet='{subnet_name}({net_name})'")
        fixed_ips_fmt = "; ".join(fixed_ip_strs)

        # Floating IPs attached to this port
        fip_strs = []
        for f in resolver.fips_by_port.get(p.id, []):
            fip = getattr(f, "floating_ip_address", "")
            ext_net_name = resolver.name_or_id("network", getattr(f, "floating_network_id", ""))
            router_name = resolver.name_or_id("router", getattr(f, "router_id", "")) if getattr(f, "router_id", None) else ""
            base = f"fip='{fip}', external_net='{ext_net_name}'"
            if router_name:
                base += f", router='{router_name}'"
            fip_strs.append(base)
        fips_fmt = "; ".join(fip_strs)

        # Security groups: names if unambiguous
        sg_names = []
        for sgid in getattr(p, "security_group_ids", None) or getattr(p, "security_groups", []) or []:
            # openstacksdk exposes either .security_group_ids (preferred) or .security_groups
            sg_names.append(resolver.name_or_id("security_group", sgid))
        secgroups_fmt = ",".join(sg_names)

        # Device binding target name
        owner = getattr(p, "device_owner", "") or ""
        device_id = getattr(p, "device_id", "") or ""
        bound_to = ""
        if device_id:
            if owner.startswith("compute"):
                bound_to = resolver.name_or_id("server", device_id)
            elif owner.startswith("network:router"):
                bound_to = resolver.name_or_id("router", device_id)
            elif "LOADBALANCER" in owner.upper() or "OCTAVIA" in owner.lower():
                bound_to = resolver.name_or_id("loadbalancer", device_id) if resolver._have_lb else device_id
            else:
                # Best-effort fallback
                for typ in ("server", "router", "loadbalancer"):
                    nm = resolver.try_name(typ, device_id)
                    if nm:
                        bound_to = nm
                        break
                bound_to = bound_to or device_id

        # Binding diagnostics
        host = get_binding_attr(p, "binding:host_id") or ""
        vif_type = get_binding_attr(p, "binding:vif_type") or ""
        vnic_type = get_binding_attr(p, "binding:vnic_type") or ""

        # Trunk diagnostics
        trunk_role = ""
        if p.id in resolver.trunk_by_parent_port:
            t = resolver.trunk_by_parent_port[p.id]
            tname = resolver.name_or_id("trunk", t.id)
            nsubs = len(getattr(t, "sub_ports", []) or [])
            trunk_role = f"TRUNK-PARENT {tname} (subports={nsubs})"
        elif p.id in resolver.subport_to_trunk:
            t = resolver.subport_to_trunk[p.id]["trunk"]
            sp = resolver.subport_to_trunk[p.id]["sp"]
            tname = resolver.name_or_id("trunk", t.id)
            segtype = sp.get("segmentation_type", "")
            segid = sp.get("segmentation_id", "")
            trunk_role = f"TRUNK-SUBPORT of {tname} [{segtype}:{segid}]"

        # QoS policy name (if any)
        qos_policy = resolver.name_or_id("qos_policy", getattr(p, "qos_policy_id", "")) if getattr(p, "qos_policy_id", None) else ""

        # Network name
        network_name = resolver.name_or_id("network", getattr(p, "network_id", ""))

        # Allowed address pairs
        aaps = getattr(p, "allowed_address_pairs", []) or []
        aap_strs = [f"{a.get('ip_address','')}({a.get('mac_address','')})" for a in aaps]
        aap_fmt = ",".join(aap_strs)

        # DNS name (and FQDN if present)
        dns_name = getattr(p, "dns_name", "") or ""
        fqdn = ""
        for da in getattr(p, "dns_assignment", []) or []:
            # dns_assignment is a list of dicts: {'hostname', 'fqdn', 'ip_address'}
            if da.get("fqdn"):
                fqdn = da["fqdn"]
                break
        dns_fmt = dns_name if dns_name else fqdn

        admin_up = getattr(p, "is_admin_state_up", None)
        if admin_up is None:
            admin_up = getattr(p, "admin_state_up", None)
        admin_up = "" if admin_up is None else ("UP" if admin_up else "DOWN")

        row = {
            "id": p.id,
            "name": getattr(p, "name", "") or "",
            "network": network_name,
            "ips": fixed_ips_fmt,
            "fips": fips_fmt,
            "status": getattr(p, "status", "") or "",
            "admin": admin_up,
            "secgroups": secgroups_fmt,
            "device_owner": owner,
            "bound_to": bound_to,
            "host": host,
            "vif_type": vif_type,
            "vnic_type": vnic_type,
            "mac": getattr(p, "mac_address", "") or "",
            "qos_policy": qos_policy,
            "port_security": "" if getattr(p, "port_security_enabled", None) is None else ("on" if p.port_security_enabled else "off"),
            "trunk": trunk_role,
            "dns": dns_fmt,
            "tags": ",".join(getattr(p, "tags", []) or []),
            "age": human_age(getattr(p, "created_at", "")),
        }
        rows.append(row)
    return rows


def print_table(rows, columns):
    # Compute column widths
    widths = {c: len(c) for c in columns}
    for r in rows:
        for c in columns:
            widths[c] = max(widths[c], len(str(r.get(c, ""))))
    # Print header
    header = "  ".join(f"{c.upper():<{widths[c]}}" for c in columns)
    sep = "  ".join("-" * widths[c] for c in columns)
    print(header)
    print(sep)
    for r in rows:
        print("  ".join(f"{str(r.get(c, '')):<{widths[c]}}" for c in columns))


def main():
    parser = argparse.ArgumentParser(description="List Neutron ports with names and diagnostics.")
    parser.add_argument("--cloud", default=None, help="Cloud name (if using clouds.yaml). Default: env vars.")
    parser.add_argument("--project-id", default=None, help="Override project ID to filter.")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of a table.")
    parser.add_argument("--csv", metavar="FILE", help="Write CSV to FILE.")
    parser.add_argument("--wide", action="store_true", help="Show more diagnostic columns.")
    parser.add_argument("--columns", default=None,
                        help="Comma-separated list of columns to show (overrides --wide).")
    parser.add_argument("--debug", action="store_true", help="Enable openstacksdk HTTP logging.")
    args = parser.parse_args()

    if args.debug:
        openstack.enable_logging(debug=True, http_debug=True)

    # Connect: prefer env vars; --cloud if provided points to clouds.yaml
    conn = openstack.connect(cloud=args.cloud)

    # Determine current project
    project_id = args.project_id
    if not project_id:
        # Try session first, fallback to OS_PROJECT_ID/OS_TENANT_ID
        try:
            project_id = conn.session.get_project_id()
        except Exception:
            project_id = os.environ.get("OS_PROJECT_ID") or os.environ.get("OS_TENANT_ID")

    if not project_id:
        print("ERROR: Could not determine current project_id. Set OS_PROJECT_ID or use --project-id.", file=sys.stderr)
        sys.exit(3)

    # Fetch ports for the current project
    ports = list(conn.network.ports(project_id=project_id))

    resolver = NameResolver(conn, project_id)
    rows = build_rows(conn, resolver, ports)

    # Default column sets
    default_cols = ["id", "bound_to", "ips", "fips", "status", "secgroups", "device_owner"]
    wide_cols = default_cols + [
        "network", "host", "vif_type", "vnic_type", "mac",
        "qos_policy", "port_security", "trunk", "dns", "tags", "admin", "age"
    ]

    if args.columns:
        columns = [c.strip() for c in args.columns.split(",") if c.strip()]
    else:
        columns = wide_cols if args.wide else default_cols

    # Output
    if args.json:
        # Only include selected columns
        out = [{c: r.get(c, "") for c in columns} for r in rows]
        print(json.dumps(out, indent=2, sort_keys=False))
    elif args.csv:
        with open(args.csv, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            for r in rows:
                writer.writerow({c: r.get(c, "") for c in columns})
        print(f"Wrote {len(rows)} rows to {args.csv}")
    else:
        print_table(rows, columns)


if __name__ == "__main__":
    main()
