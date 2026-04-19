"""Unit tests for LAN discovery helpers (no real network I/O)."""

import ipaddress

from spyoncino.discovery_lan import (
    dedupe_hosts_preserve_order,
    expand_networks_to_hosts,
    parse_cidr_list,
)


def test_dedupe_hosts() -> None:
    assert dedupe_hosts_preserve_order(["a", "b", "a"]) == ["a", "b"]


def test_parse_cidr_list() -> None:
    nets = parse_cidr_list("192.168.1.0/30, 10.0.0.0/32")
    assert len(nets) == 2
    assert nets[0] == ipaddress.ip_network("192.168.1.0/30", strict=False)


def test_expand_networks() -> None:
    n = ipaddress.ip_network("192.168.1.0/30", strict=False)
    hosts, trunc = expand_networks_to_hosts([n], max_hosts=100)
    assert not trunc
    assert "192.168.1.1" in hosts
    assert "192.168.1.2" in hosts


def test_expand_slash32() -> None:
    n = ipaddress.ip_network("10.0.0.5/32", strict=False)
    hosts, trunc = expand_networks_to_hosts([n], max_hosts=10)
    assert hosts == ["10.0.0.5"]
    assert not trunc
