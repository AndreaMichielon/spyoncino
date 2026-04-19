"""
LAN discovery: derive local IPv4 subnets, expand to host addresses, TCP-scan RTSP port.

Used when the user opts in to “scan local subnets” for camera discovery.
Requires ``psutil`` (listed in project dependencies).
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import suppress
from dataclasses import dataclass, field

_logger = logging.getLogger(__name__)


@dataclass
class LanMergeResult:
    """Result of merging manual hosts with optional LAN TCP scan."""

    merged_hosts: list[str]
    messages: list[str] = field(default_factory=list)
    scanned_networks: list[str] = field(default_factory=list)
    candidate_ips: int = 0
    tcp_open_count: int = 0
    truncated: bool = False


try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None


def psutil_available() -> bool:
    return psutil is not None


def local_ipv4_networks() -> list[ipaddress.IPv4Network]:
    """Non-loopback IPv4 networks from this machine's interfaces."""
    if psutil is None:
        raise RuntimeError("psutil is not installed")
    seen: dict[str, ipaddress.IPv4Network] = {}
    for addrs in psutil.net_if_addrs().values():
        for a in addrs:
            if a.family != socket.AF_INET or not a.address or not a.netmask:
                continue
            if a.address.startswith("127."):
                continue
            try:
                iface = ipaddress.IPv4Interface(f"{a.address}/{a.netmask}")
                net = iface.network
                seen[str(net)] = net
            except ValueError:
                continue
    return list(seen.values())


def parse_cidr_list(text: str) -> list[ipaddress.IPv4Network]:
    out: list[ipaddress.IPv4Network] = []
    for part in re.split(r"[\s,;]+", text or ""):
        p = part.strip()
        if not p:
            continue
        try:
            net = ipaddress.ip_network(p, strict=False)
            if isinstance(net, ipaddress.IPv4Network):
                out.append(net)
        except ValueError:
            _logger.debug("Invalid CIDR skipped: %s", p)
    return out


def dedupe_hosts_preserve_order(hosts: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for h in hosts:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def expand_networks_to_hosts(
    networks: list[ipaddress.IPv4Network],
    max_hosts: int,
) -> tuple[list[str], bool]:
    """
    Return (host_ips, truncated) for all `.hosts()` in each network until cap.
    """
    hosts: list[str] = []
    for net in networks:
        try:
            if net.prefixlen == 32:
                addrs = [str(net.network_address)]
            else:
                addrs = [str(h) for h in net.hosts()]
            for a in addrs:
                if len(hosts) >= max_hosts:
                    return hosts, True
                hosts.append(a)
        except ValueError:
            continue
    return hosts, False


def tcp_port_open(ip: str, port: int, timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def scan_hosts_tcp_port(
    ips: list[str],
    port: int,
    timeout: float,
    max_workers: int = 56,
) -> list[str]:
    """Return sorted IPs where TCP `port` accepts a connection."""
    if not ips:
        return []
    open_ips: list[str] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(tcp_port_open, ip, port, timeout): ip for ip in ips}
        for fut in as_completed(futs):
            ip = futs[fut]
            with suppress(Exception):
                if fut.result():
                    open_ips.append(ip)
    open_ips.sort(key=lambda x: int(ipaddress.IPv4Address(x)))
    return open_ips


def merge_manual_and_lan_hosts(
    manual_hosts: list[str],
    scan_local_subnets: bool,
    extra_cidrs_text: str,
    rtsp_port: int,
    tcp_probe_timeout: float,
    max_scan_hosts: int,
) -> LanMergeResult:
    """
    If ``scan_local_subnets``, add IPv4 subnets from interfaces + optional CIDRs,
    expand up to ``max_scan_hosts`` addresses, TCP-probe ``rtsp_port``, merge with manual.
    """
    manual_deduped = dedupe_hosts_preserve_order(list(manual_hosts))
    notes: list[str] = []
    if not scan_local_subnets:
        return LanMergeResult(merged_hosts=manual_deduped)

    if psutil is None:
        notes.append("LAN scan skipped: psutil not available.")
        return LanMergeResult(merged_hosts=manual_deduped, messages=notes)

    nets: list[ipaddress.IPv4Network] = []
    try:
        nets.extend(local_ipv4_networks())
    except Exception as e:
        _logger.debug("local_ipv4_networks: %s", e)
        notes.append(f"Could not read local interfaces: {e}")

    extra = parse_cidr_list(extra_cidrs_text)
    nets.extend(extra)
    nets = list({str(n): n for n in nets}.values())
    network_labels = sorted({str(n) for n in nets})

    if not nets:
        notes.append("LAN scan: no IPv4 subnets (check interfaces or extra CIDRs).")
        return LanMergeResult(
            merged_hosts=manual_deduped,
            messages=notes,
            scanned_networks=[],
        )

    candidates, truncated = expand_networks_to_hosts(nets, max_scan_hosts)
    if truncated:
        notes.append(
            f"LAN host list capped at {max_scan_hosts} addresses (add narrower CIDRs to focus)."
        )

    if not candidates:
        return LanMergeResult(
            merged_hosts=manual_deduped,
            messages=notes,
            scanned_networks=network_labels,
            candidate_ips=0,
            truncated=truncated,
        )

    open_ips = scan_hosts_tcp_port(candidates, rtsp_port, timeout=tcp_probe_timeout)
    merged = dedupe_hosts_preserve_order(list(manual_hosts) + open_ips)
    return LanMergeResult(
        merged_hosts=merged,
        messages=notes,
        scanned_networks=network_labels,
        candidate_ips=len(candidates),
        tcp_open_count=len(open_ips),
        truncated=truncated,
    )
