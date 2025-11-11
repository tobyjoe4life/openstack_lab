#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import os
import re
import sys
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Tuple

try:
    import openstack
    from openstack import exceptions as os_exc
except Exception:
    print("ERROR: This script requires 'openstacksdk' inside your virtualenv.", file=sys.stderr)
    print("       pip install openstacksdk", file=sys.stderr)
    raise

# NEW: import keystoneauth access helpers just for role names from token
try:
    from keystoneauth1 import access as ks_access  # noqa: F401
except Exception:
    ks_access = None  # we'll still run, just fall back

# ------------------------------- ANSI colors ---------------------------------

class Palette:
    def __init__(self, enabled: bool):
        self.enabled = enabled and sys.stdout.isatty() and not os.environ.get("NO_COLOR")

        def c(code: str) -> str:
            return code if self.enabled else ""

        self.RESET = c("\033[0m")
        self.BOLD = c("\033[1m")
        self.DIM = c("\033[2m")
        self.CYAN = c("\033[36m")
        self.GREEN = c("\033[32m")
        self.YELLOW = c("\033[33m")
        self.RED = c("\033[31m")
        self.MAGENTA = c("\033[35m")
        self.GRAY = c("\033[90m")

    def colorize_status(self, s: str) -> str:
        key = (s or "").strip().upper()
        good = {"ACTIVE", "UP", "ENABLED", "AVAILABLE", "RUNNING", "ONLINE"}
        warn = {"DOWN", "BUILD", "SHUTOFF", "PAUSED", "SUSPENDED", "DELETING", "RESIZING", "QUEUED"}
        bad = {"ERROR", "CRASHED", "DEGRADED", "FAILED", "OFFLINE"}

        if key in good:
            return f"{self.GREEN}{s}{self.RESET}"
        if key in warn:
            return f"{self.YELLOW}{s}{self.RESET}"
        if key in bad:
            return f"{self.RED}{s}{self.RESET}"
        return s

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s or "")

def limit_str(v: Optional[int]) -> str:
    if v in (-1, None):
        return "∞"
    return str(v)

def wrap_bullet_line(text: str, max_width: int, indent: int = 2) -> str:
    if max_width <= 0:
        return text
    segs = text.split(" • ")
    lines: List[str] = []
    cur = ""
    for seg in segs:
        candidate = seg if not cur else f"{cur} • {seg}"
        if len(strip_ansi(candidate)) <= max_width:
            cur = candidate
            continue
        if cur:
            lines.append(cur)
        parts = seg.split(", ")
        cur2 = ""
        for part in parts:
            cand2 = part if not cur2 else f"{cur2}, {part}"
            if len(strip_ansi(cand2)) <= max_width - indent:
                cur2 = cand2
            else:
                if cur2:
                    lines.append(" " * indent + cur2)
                cur2 = part
        if cur2:
            lines.append(" " * indent + cur2)
        cur = ""
    if cur:
        lines.append(cur)
    return "\n".join(lines)

POWER_STATE_MAP = {0: "NOSTATE", 1: "RUNNING", 3: "PAUSED", 4: "SHUTDOWN", 6: "CRASHED", 7: "SUSPENDED", 8: "UNKNOWN"}
ROUTER_IF_OWNERS = ("network:router_interface","network:router_interface_distributed","network:ha_router_replicated_interface")
EXCLUDE_FREE_STANDING_PREFIXES = ("compute:","network:","octavia:","trunk:")

def boolstr(v: Optional[bool]) -> str:
    return "on" if v else "off"

def gw_suffix(gw: Optional[str]) -> str:
    if not gw:
        return "-"
    try:
        ip = ipaddress.ip_address(gw)
        if ip.version == 4:
            return "." + gw.split(".")[-1]
        compressed = ip.compressed
        last = compressed.split(":")[-1] or "0"
        return f"::{last}"
    except Exception:
        return "?"

def size_mb(bytes_val: Optional[int]) -> int:
    if bytes_val is None:
        return 0
    return int(round(bytes_val / (1024.0 * 1024.0)))

def parse_backend_from_host(host: Optional[str]) -> str:
    if not host or "@" not in host:
        return "-"
    try:
        after_at = host.split("@", 1)[1]
        return after_at.split("#", 1)[0]
    except Exception:
        return "-"

def sort_by_name(items: Iterable) -> List:
    return sorted(items, key=lambda o: (getattr(o, "name", "") or "").lower())

def compute_usage(conn, project_id, servers, flavor_by_id):
    """
    Return (instances_used, cores_used, ram_used_mb) as robustly as possible.
    Order of preference:
      1) Nova absolute limits (total*Used)
      2) Quota set with usage/detail (cores_in_use/ram_in_use/instances_in_use or nested dicts)
      3) Sum from servers, using embedded flavor numbers or flavor lookup
    """
    # 1) Nova absolute limits
    try:
        limits = conn.compute.get_limits()
        abs_lim = getattr(limits, "absolute", limits)
        def getv(k):
            return (getattr(abs_lim, k, None) if hasattr(abs_lim, k)
                    else abs_lim.get(k, None) if hasattr(abs_lim, "get") else None)
        iu = getv("totalInstancesUsed"); cu = getv("totalCoresUsed"); ru = getv("totalRAMUsed")
        if iu is not None and cu is not None and ru is not None:
            return int(iu), int(cu), int(ru)
    except Exception:
        pass

    # 2) Quota set with usage
    try:
        qset = conn.compute.get_quota_set(project_id, usage=True)  # some SDKs accept usage/detail
        qd = {}
        try:
            qd = qset.to_dict()
        except Exception:
            if isinstance(qset, dict):
                qd = qset

        def pick_in_use(name):
            # Try flat "<name>_in_use"
            v = qd.get(f"{name}_in_use")
            if v is not None:
                return int(v)
            # Try nested dict: "<name>": {"in_use": N}
            sub = qd.get(name) or getattr(qset, name, None)
            if isinstance(sub, dict):
                if "in_use" in sub:
                    return int(sub["in_use"])
                if "used" in sub:
                    return int(sub["used"])
            # Try attribute form
            v = getattr(qset, f"{name}_in_use", None)
            return int(v) if v is not None else None

        iu = pick_in_use("instances")
        cu = pick_in_use("cores")
        ru = pick_in_use("ram")
        if iu is not None and cu is not None and ru is not None:
            return iu, cu, ru
    except Exception:
        pass

    # 3) Fallback: sum from servers
    live = [s for s in servers if (getattr(s, "status", "") or "").upper() not in ("DELETED", "SOFT_DELETED")]
    iu = len(live)
    cu = 0
    ru = 0
    for s in live:
        vcpus = None
        ram_mb = None
        # Prefer embedded numbers (present with many microversions)
        try:
            if isinstance(s.flavor, dict):
                vcpus = s.flavor.get("vcpus")
                ram_mb = s.flavor.get("ram")
        except Exception:
            pass
        # If missing, try to resolve by id
        if vcpus is None or ram_mb is None:
            fid = None
            try:
                fid = s.flavor.get("id") if isinstance(s.flavor, dict) else getattr(s.flavor, "id", None)
            except Exception:
                fid = None
            if fid:
                fobj = flavor_by_id.get(fid)
                if not fobj:
                    try:
                        fobj = conn.compute.get_flavor(fid)
                        if fobj:
                            flavor_by_id[fobj.id] = fobj
                    except Exception:
                        fobj = None
                if fobj:
                    if vcpus is None:
                        vcpus = getattr(fobj, "vcpus", 0)
                    if ram_mb is None:
                        ram_mb = getattr(fobj, "ram", 0)
        cu += int(vcpus or 0)
        ru += int(ram_mb or 0)
    return iu, cu, ru


# ------------------------------- main routine --------------------------------

def main():
    parser = argparse.ArgumentParser(description="Compact OpenStack project overview")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colors")
    parser.add_argument("--max-width", type=int, default=120, help="Wrap lines to this width (0 disables wrapping)")
    args = parser.parse_args()
    pal = Palette(enabled=not args.no_color)

    try:
        conn = openstack.connect()
    except Exception:
        print("ERROR: could not establish OpenStack connection. Have you 'source'd your openrc?", file=sys.stderr)
        raise

    project_id = getattr(conn, "current_project_id", None)
    user_id = getattr(conn, "current_user_id", None)

    project = conn.identity.get_project(project_id) if project_id else None
    user = conn.identity.get_user(user_id) if user_id else None

    domain = None
    if project is not None and getattr(project, "domain_id", None):
        try:
            domain = conn.identity.get_domain(project.domain_id)
        except Exception:
            domain = None

    role_names: List[str] = []
    try:
        # This does NOT require admin; it introspects the token in memory.
        auth_ref = conn.session.auth.get_auth_ref(conn.session)
        # keystoneauth1.access.AccessInfoV3 has 'role_names' and 'roles'
        role_names = list(getattr(auth_ref, "role_names", []) or [])
        if not role_names:
            roles = getattr(auth_ref, "roles", []) or []
            role_names = [r.get("name") for r in roles if isinstance(r, dict) and r.get("name")]
    except Exception:
        # Fallback to role assignments (may require elevated policy)
        try:
            assignments = list(conn.identity.role_assignments(user=user_id, project=project_id))
            role_ids = [a.role["id"] for a in assignments if getattr(a, "role", None)]
            all_roles = {r.id: r.name for r in conn.identity.roles()}
            role_names = [all_roles.get(rid, rid) for rid in role_ids]
        except Exception:
            role_names = []

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(
        f"{pal.BOLD}OpenStack Project Overview{pal.RESET}  "
        f"({pal.CYAN}Epoxy 2025.1{pal.RESET})  [{now}]"
    )
    print(
        f"Identity: domain={getattr(domain, 'name', 'Unknown')} • "
        f"project={getattr(project, 'name', 'Unknown')} • "
        f"user={getattr(user, 'name', 'Unknown')} • "
        f"roles={', '.join(role_names) if role_names else '-'}"
    )
    print()

    # ----------------------------- Networking -----------------------------
    try:
        all_networks = list(conn.network.networks())  # visible to project
        all_subnets = list(conn.network.subnets())
    except Exception:
        all_networks, all_subnets = [], []

    networks_by_id: Dict[str, object] = {n.id: n for n in all_networks}
    subnets_by_id: Dict[str, object] = {s.id: s for s in all_subnets}

    try:
        project_ports = list(conn.network.ports(project_id=project_id))
    except Exception:
        project_ports = []
    try:
        project_routers = list(conn.network.routers(project_id=project_id))
    except Exception:
        project_routers = []
    try:
        project_sgs = list(conn.network.security_groups(project_id=project_id))
    except Exception:
        project_sgs = []
    try:
        project_fips = list(conn.network.ips(project_id=project_id))
    except Exception:
        try:
            project_fips = list(conn.network.floating_ips(project_id=project_id))
        except Exception:
            project_fips = []

    def is_external(n) -> bool:
        return bool(getattr(n, "is_router_external", getattr(n, "router_external", False)))

    provider_nets = [n for n in all_networks if getattr(n, "is_shared", False) and not is_external(n)]
    external_nets  = [n for n in all_networks if is_external(n)]
    internal_nets  = [n for n in all_networks if getattr(n, "project_id", None) == project_id and not is_external(n) and not getattr(n, "is_shared", False)]

    def nq(quota_obj, main: str, alt: Optional[str] = None) -> Optional[int]:
        if not quota_obj:
            return None
        if hasattr(quota_obj, main):
            return getattr(quota_obj, main)
        if alt and hasattr(quota_obj, alt):
            return getattr(quota_obj, alt)
        try:
            return quota_obj.get(main) or (quota_obj.get(alt) if alt else None)
        except Exception:
            return None

    try:
        neutron_quota = conn.network.get_quota(project_id)
    except Exception:
        neutron_quota = None

    nets_used = len([n for n in all_networks if getattr(n, "project_id", None) == project_id and not is_external(n)])
    subs_used = len([s for s in all_subnets if getattr(s, "project_id", None) == project_id])
    rtr_used  = len(project_routers)
    ports_used = len(project_ports)
    sgs_used = len(project_sgs)
    fips_used = len(project_fips)

    nets_lim = nq(neutron_quota, "network", "networks")
    subs_lim = nq(neutron_quota, "subnet", "subnets")
    rtr_lim  = nq(neutron_quota, "router", "routers")
    ports_lim = nq(neutron_quota, "port", "ports")
    sgs_lim  = nq(neutron_quota, "security_group", "security_groups")
    fips_lim = nq(neutron_quota, "floatingip", "floating_ips")

    print(
        f"{pal.BOLD}NEUTRON — Networking{pal.RESET}  "
        f"(networks {nets_used}/{limit_str(nets_lim)} • "
        f"subnets {subs_used}/{limit_str(subs_lim)} • "
        f"routers {rtr_used}/{limit_str(rtr_lim)} • "
        f"ports {ports_used}/{limit_str(ports_lim)} • "
        f"secgroups {sgs_used}/{limit_str(sgs_lim)} • "
        f"fips {fips_used}/{limit_str(fips_lim)})"
    )

    def format_net_line(n) -> str:
        status = pal.colorize_status((getattr(n, "status", None) or "").upper() or "-")
        admin  = pal.colorize_status("UP" if getattr(n, "is_admin_state_up", getattr(n, "admin_state_up", False)) else "DOWN")
        sids = getattr(n, "subnet_ids", []) or []
        subs = [subnets_by_id.get(sid) for sid in sids if sid in subnets_by_id]
        subbits: List[str] = []
        for s in sort_by_name(subs):
            pools = getattr(s, "allocation_pools", None) or []
            pools_s = ";".join([f"{p.get('start')}-{p.get('end')}" for p in pools]) or "-"
            dhcp = "on" if getattr(s, "is_dhcp_enabled", getattr(s, "enable_dhcp", False)) else "off"
            gw = gw_suffix(getattr(s, "gateway_ip", None))
            subbits.append(f"{getattr(s, 'name', s.id if s else '-')}"
                           f" {getattr(s, 'cidr', '-')}"
                           f" gw:{gw} dhcp:{dhcp} pools:{pools_s}")
        subs_text = " | ".join(subbits) if subbits else "-"
        return f"  ▸ {getattr(n, 'name', n.id)}        [{status}|{admin}]   subnets: {subs_text}"

    provider_nets = sort_by_name(provider_nets)
    external_nets = sort_by_name(external_nets)
    internal_nets = sort_by_name(internal_nets)

    if provider_nets:
        print("Provider networks:")
        for n in provider_nets:
            print(wrap_bullet_line(format_net_line(n), args.max_width))
    if external_nets:
        print("External networks:")
        for n in external_nets:
            print(wrap_bullet_line(format_net_line(n), args.max_width))
    if internal_nets:
        print("Internal networks:")
        for n in internal_nets:
            print(wrap_bullet_line(format_net_line(n), args.max_width))

    print()
    print(f"Routers ({rtr_used}/{limit_str(rtr_lim)}):")

    ports_by_router: Dict[str, List] = defaultdict(list)
    for p in project_ports:
        if getattr(p, "device_id", None) and getattr(p, "device_owner", ""):
            if any(p.device_owner.startswith(prefix) for prefix in ROUTER_IF_OWNERS):
                ports_by_router[p.device_id].append(p)

    for r in sort_by_name(project_routers):
        status = pal.colorize_status((getattr(r, "status", None) or "").upper() or "-")
        egw = getattr(r, "external_gateway_info", None) or {}
        ext_net_id = egw.get("network_id")
        ext_net_name = networks_by_id.get(ext_net_id).name if ext_net_id in networks_by_id else "-"
        interfaces = []
        for p in ports_by_router.get(r.id, []):
            for f in getattr(p, "fixed_ips", []) or []:
                sid = f.get("subnet_id")
                s = subnets_by_id.get(sid)
                if not s:
                    continue
                net = networks_by_id.get(getattr(s, "network_id", None))
                netname = getattr(net, "name", net.id if net else "-")
                interfaces.append(f"{netname}/{getattr(s, 'name', s.id if s else '-')}")
        ifs_text = ", ".join(sorted(interfaces)) if interfaces else "-"
        print(wrap_bullet_line(f"  ▸ {getattr(r, 'name', r.id)}            [{status}]  ext: {ext_net_name}  ifs:[{ifs_text}]",
                               args.max_width))

    print()
    free_ports: List[object] = []
    for p in project_ports:
        owner = (getattr(p, "device_owner", "") or "").strip()
        if owner and any(owner.startswith(prefix) for prefix in EXCLUDE_FREE_STANDING_PREFIXES):
            continue
        if owner:
            continue
        free_ports.append(p)

    print(f"Free-standing ports ({len(free_ports)}):")
    for p in sort_by_name(free_ports):
        status = pal.colorize_status((getattr(p, "status", None) or "").upper() or "-")
        name = getattr(p, "name", "") or "<no-name>"
        pieces = []
        for f in getattr(p, "fixed_ips", []) or []:
            sid = f.get("subnet_id"); ip = f.get("ip_address")
            s = subnets_by_id.get(sid)
            net = networks_by_id.get(getattr(s, "network_id", None)) if s else None
            pieces.append(f"{getattr(net,'name','-')}/{getattr(s,'name','-')} {ip}")
        piece = "; ".join(pieces) if pieces else "-"
        print(wrap_bullet_line(f"  ▸ {name:<18} [{status}]  {piece}", args.max_width))

    print()
    print(f"Security groups ({sgs_used}/{limit_str(sgs_lim)}):")

    sg_name_by_id = {sg.id: sg.name for sg in project_sgs}
    sg_rules_by_sg_and_dir: Dict[Tuple[str, str], List[object]] = defaultdict(list)
    try:
        all_rules = list(conn.network.security_group_rules())
        for r in all_rules:
            sg_id = getattr(r, "security_group_id", None)
            if sg_id in sg_name_by_id:
                direction = getattr(r, "direction", "ingress")
                sg_rules_by_sg_and_dir[(sg_id, direction)].append(r)
    except Exception:
        pass

    def compact_rules(rules: List[object]) -> str:
        groups: Dict[Tuple[str, str, str], List[str]] = defaultdict(list)
        for r in rules:
            proto = getattr(r, "protocol", None) or "any"
            eth = getattr(r, "ethertype", getattr(r, "ether_type", None)) or "-"
            if getattr(r, "remote_ip_prefix", None):
                remote = r.remote_ip_prefix
            elif getattr(r, "remote_group_id", None):
                remote = "sg:" + sg_name_by_id.get(r.remote_group_id, r.remote_group_id)
            else:
                remote = "any"
            pmin = getattr(r, "port_range_min", None)
            pmax = getattr(r, "port_range_max", None)
            if proto in ("tcp", "udp"):
                if pmin and pmax and pmin == pmax:
                    token = str(pmin)
                elif pmin and pmax:
                    token = f"{pmin}-{pmax}"
                else:
                    token = "any"
            elif proto.startswith("icmp"):
                token = "icmp"
            else:
                token = "any"
            key = (proto, remote, eth)
            groups[key].append(token)
        parts: List[str] = []
        for (proto, remote, eth), tokens in groups.items():
            uniq = []
            for t in tokens:
                if t not in uniq:
                    uniq.append(t)
            port_repr = ",".join(uniq) if uniq else "any"
            if proto in ("any",):
                seg = f"any to {remote} {eth}"
            elif proto.startswith("icmp"):
                seg = f"icmp from {remote} {eth}"
            else:
                seg = f"{proto}:{port_repr} from {remote} {eth}"
            parts.append(seg)
        return "; ".join(parts) if parts else "-"

    for sg in sort_by_name(project_sgs):
        ingress = compact_rules(sg_rules_by_sg_and_dir.get((sg.id, "ingress"), []))
        egress  = compact_rules(sg_rules_by_sg_and_dir.get((sg.id, "egress"), []))
        print(wrap_bullet_line(f"  ▸ {sg.name}  (ingress: {ingress} | egress: {egress})", args.max_width))

    print()
    free_fips = [f for f in project_fips if not getattr(f, "port_id", None)]
    print(f"Free-standing Floating IPs ({len(free_fips)}/{limit_str(fips_lim)}):")
    for f in sort_by_name(free_fips):
        status = pal.colorize_status((getattr(f, "status", None) or "").upper() or "-")
        net_name = "-"
        try:
            extn = networks_by_id.get(getattr(f, "floating_network_id", None))
            net_name = getattr(extn, "name", "-")
        except Exception:
            pass
        print(wrap_bullet_line(
            f"  ▸ {getattr(f, 'floating_ip_address', '-'):<15}  ext-net: {net_name}  [{status}]  (not associated)",
            args.max_width))

    # ------------------------------- Compute -------------------------------
    print()

    try:
        flavors = list(conn.compute.flavors())
    except Exception:
        flavors = []
    try:
        images = list(conn.image.images())
    except Exception:
        images = []
    try:
        keypairs = list(conn.compute.keypairs())
    except Exception:
        keypairs = []
    try:
        servers = list(conn.compute.servers(details=True))
    except Exception:
        servers = []

    flavor_by_id = {f.id: f for f in flavors}
    image_name_by_id = {i.id: i.name for i in images}
    live_servers = [s for s in servers if (getattr(s, "status", "") or "").upper() not in ("DELETED", "SOFT_DELETED")]

    # --- Compute quotas (max) from quota set ---
    max_instances = max_cores = max_ram_mb = None
    try:
        qset = conn.compute.get_quota_set(project_id)
        max_instances = getattr(qset, "instances", None)
        max_cores     = getattr(qset, "cores", None)
        max_ram_mb    = getattr(qset, "ram", None)  # MB
    except Exception:
        # Fallback: limits maxima
        try:
            limits = conn.compute.get_limits()
            abs_lim = getattr(limits, "absolute", limits)
            def getv(k):
                return (getattr(abs_lim, k, None) if hasattr(abs_lim, k)
                        else abs_lim.get(k, None) if hasattr(abs_lim, "get") else None)
            max_instances = getv("maxTotalInstances")
            max_cores     = getv("maxTotalCores")
            max_ram_mb    = getv("maxTotalRAMSize")
        except Exception:
            pass

    # --- Compute usage (used instances, cores, ram) robustly ---
    used_instances, used_vcpus, used_ram_mb = compute_usage(conn, project_id, servers, flavor_by_id)

    compute_header = (
        f"{pal.BOLD}NOVA — Compute{pal.RESET}  "
        f"(instances {used_instances}/{limit_str(max_instances)} • "
        f"vcpus {used_vcpus}/{limit_str(max_cores)} • "
        f"ram {used_ram_mb}/{limit_str(max_ram_mb)} MB)"
    )
    print(compute_header)


    if flavors:
        print(f"Flavors ({len(flavors)} visible):")
        for f in sort_by_name(flavors):
            ephem = getattr(f, "ephemeral", getattr(f, "OS-FLV-EXT-DATA:ephemeral", 0)) or 0
            pub = "public" if getattr(f, "is_public", True) else "private"
            print(wrap_bullet_line(
                f"  ▸ {f.name:<12} vCPU={getattr(f,'vcpus',0)} RAM={getattr(f,'ram',0)} Disk={getattr(f,'disk',0)} Ephem={ephem} {pub}",
                args.max_width))

    if images:
        print(f"\nImages ({len(images)}):")

        def min_flavor_for(img) -> str:
            need_ram = int(getattr(img, "min_ram", 0) or 0)
            need_disk = int(getattr(img, "min_disk", 0) or 0)
            candidates = [f for f in flavors if int(getattr(f, "ram", 0)) >= need_ram and int(getattr(f, "disk", 0)) >= need_disk]
            if not candidates:
                return "none"
            candidates.sort(key=lambda x: (int(getattr(x, "ram", 0)), int(getattr(x, "disk", 0)),
                                           int(getattr(x, "vcpus", 0)), x.name))
            return candidates[0].name

        for i in sort_by_name(images):
            name = getattr(i, "name", i.id)
            status = pal.colorize_status((getattr(i, "status", None) or "").upper() or "-")
            disk_fmt = getattr(i, "disk_format", "-")
            cont_fmt = getattr(i, "container_format", "-")
            size = size_mb(getattr(i, "size", None))
            vis = getattr(i, "visibility", getattr(i, "is_public", None))
            if isinstance(vis, bool):
                vis = "public" if vis else "private"
            min_ram = int(getattr(i, "min_ram", 0) or 0)
            min_disk = int(getattr(i, "min_disk", 0) or 0)
            minflv = min_flavor_for(i)
            print(wrap_bullet_line(
                f"  ▸ {name:<22} {status}  "
                f"disk/container={disk_fmt}/{cont_fmt}  size={size}MB  vis={vis}  "
                f"min:{min_ram}/{min_disk}  min-flavor:{minflv}",
                args.max_width))

    if keypairs:
        print(f"\nKeypairs ({len(keypairs)}):")
        parts = [f"{k.name} ({getattr(k, 'type', getattr(k, 'key_type', '-') )})" for k in sort_by_name(keypairs)]
        print("  ▸ " + "   ▸ ".join(parts))

    # Build caches for ports/FIPs/volumes -> servers
    ports_by_server: Dict[str, List] = defaultdict(list)
    for p in project_ports:
        sid = getattr(p, "device_id", None)
        if sid:
            ports_by_server[sid].append(p)

    fip_by_port: Dict[str, List] = defaultdict(list)
    for f in project_fips:
        pid = getattr(f, "port_id", None)
        if pid:
            fip_by_port[pid].append(f)

    try:
        volumes = list(conn.block_storage.volumes(details=True))
    except Exception:
        volumes = []

    vols_by_server: Dict[str, List[object]] = defaultdict(list)
    for v in volumes:
        for a in getattr(v, "attachments", []) or []:
            sid = a.get("server_id") or a.get("serverId")
            if sid:
                vols_by_server[sid].append(v)

    print(f"\nInstances ({used_instances}/{limit_str(max_instances)}):")
    for s in sort_by_name(live_servers):
        name = getattr(s, "name", s.id)
        status = pal.colorize_status((getattr(s, "status", None) or "").upper() or "-")
        sdict = {}
        try:
            sdict = s.to_dict()
        except Exception:
            pass
        vm_state = sdict.get("OS-EXT-STS:vm_state", getattr(s, "vm_state", None)) or "-"
        pstate = getattr(s, "power_state", None)
        pstate_name = POWER_STATE_MAP.get(pstate, str(pstate) if pstate is not None else "-")
        host = sdict.get("OS-EXT-SRV-ATTR:host", getattr(s, "host", None)) or "-"

        img_name = "-"
        try:
            if isinstance(s.image, dict):
                iid = s.image.get("id")
                img_name = image_name_by_id.get(iid, iid or "-")
            elif getattr(s, "image", None):
                iid = getattr(s.image, "id", None)
                img_name = image_name_by_id.get(iid, iid or "-")
            else:
                img_name = "volume-boot"
        except Exception:
            img_name = "volume-boot" if not getattr(s, "image", None) else "-"

        flv_name = "-"
        try:
            fid = s.flavor.get("id") if isinstance(s.flavor, dict) else getattr(s.flavor, "id", None)
            fobj = resolve_flavor(fid)
            if fobj:
                flv_name = getattr(fobj, "name", "-")
            else:
                flv_name = s.flavor.get("original_name") if isinstance(s.flavor, dict) else "-"
        except Exception:
            pass

        keyname = getattr(s, "key_name", None) or "-"

        print(wrap_bullet_line(
            f"  ▸ {name:<12} [{status}({vm_state})] power={pstate_name} host={host} image={img_name} flavor={flv_name} key={keyname}",
            args.max_width
        ))

        port_bits: List[str] = []
        sg_names: set = set()
        for p in ports_by_server.get(s.id, []):
            for sgid in getattr(p, "security_group_ids", []) or []:
                sg_names.add(sg_name_by_id.get(sgid, sgid))
            for f in getattr(p, "fixed_ips", []) or []:
                sid = f.get("subnet_id")
                ipaddr = f.get("ip_address")
                sub = subnets_by_id.get(sid)
                net = networks_by_id.get(getattr(sub, "network_id", None)) if sub else None
                netname = getattr(net, "name", "-"); subname = getattr(sub, "name", "-")
                fiptext = ""
                for fx in fip_by_port.get(getattr(p, "id", None), []):
                    fiptext = f" (fip:{getattr(fx, 'floating_ip_address', '-')})"
                    break
                port_bits.append(f"{netname}/{subname} {ipaddr}{fiptext}")

        vol_bits: List[str] = []
        for v in vols_by_server.get(s.id, []):
            vname = getattr(v, "name", v.id)
            vsize = getattr(v, "size", 0)
            vol_bits.append(f"{vname}:{vsize}GB")

        print(wrap_bullet_line(
            "      ports:[" + ("; ".join(port_bits) if port_bits else "-") + "] • "
            "vols:[" + ("; ".join(vol_bits) if vol_bits else "-") + "] • "
            "sgs:[" + (", ".join(sorted(sg_names)) if sg_names else "-") + "]",
            args.max_width, indent=6
        ))

    # ---------------------------- Block Storage -----------------------------
    print()
    c_vols_lim = c_snaps_lim = c_baks_lim = c_gib_lim = None
    try:
        qset = conn.block_storage.get_quota_set(project_id)
        c_vols_lim = getattr(qset, "volumes", None)
        c_snaps_lim = getattr(qset, "snapshots", None)
        c_baks_lim = getattr(qset, "backups", None)
        c_gib_lim  = getattr(qset, "gigabytes", None)
    except Exception:
        pass

    try:
        snapshots = list(conn.block_storage.snapshots(details=True))
    except Exception:
        snapshots = []
    try:
        backups = list(conn.block_storage.backups(details=True))
        backups_supported = True
    except Exception:
        backups = []
        backups_supported = False

    try:
        volumes  # from above
    except NameError:
        volumes = []

    vols_used = len(volumes)
    snaps_used = len(snapshots)
    baks_used = len(backups) if backups_supported else 0
    gib_used = sum(int(getattr(v, "size", 0) or 0) for v in volumes)

    print(
        f"{pal.BOLD}CINDER — Block Storage{pal.RESET}  "
        f"(vols {vols_used}/{limit_str(c_vols_lim)} • "
        f"snaps {snaps_used}/{limit_str(c_snaps_lim)} • "
        f"backups {(baks_used if backups_supported else 'n/a')}/{(limit_str(c_baks_lim) if backups_supported else 'n/a')} • "
        f"gib {gib_used}/{limit_str(c_gib_lim)})"
    )

    server_name_by_id = {s.id: getattr(s, "name", s.id) for s in live_servers}
    for v in sort_by_name(volumes):
        vname = getattr(v, "name", v.id)
        vtype = getattr(v, "volume_type", "-")
        vsize = getattr(v, "size", 0)
        vstatus = pal.colorize_status((getattr(v, "status", None) or "").upper() or "-")
        backend = parse_backend_from_host(getattr(v, "host", None) or getattr(v, "os-vol-host-attr:host", None))
        atts = [server_name_by_id.get(a.get("server_id") or a.get("serverId"), "-") for a in getattr(v, "attachments", []) or []]
        attached_to = "[" + ("; ".join(atts) if atts else "") + "]" if atts else "-"
        snaps_n = len([s for s in snapshots if getattr(s, "volume_id", None) == v.id])
        baks_n  = len([b for b in backups if getattr(b, "volume_id", None) == v.id]) if backups_supported else "n/a"
        print(wrap_bullet_line(
            f"  ▸ {vname:<14} type={vtype} size={vsize}GB  {vstatus:<9} "
            f"attached:{attached_to:<18} backend={backend:<12} snaps={snaps_n} backups={baks_n}",
            args.max_width))

    print()

if __name__ == "__main__":
    main()
