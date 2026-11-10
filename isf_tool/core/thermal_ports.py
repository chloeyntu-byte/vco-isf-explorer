"""Physical noise ports from Spectre terminal evidence, not TB naming rules."""

from __future__ import annotations

import re


def parse_terminal_map(text):
    """Read ``info what=nodes`` (including hierarchical primitive names)."""
    records = {}
    current = None
    for line in text.splitlines():
        match = re.match(r"^([A-Za-z0-9_]+):\s+(\S+)(?:\s+of\s+(\S+))?\s*$", line)
        if match:
            master, device, parent = match.groups()
            if device in records:
                raise ValueError("duplicate terminal-map device: " + device)
            current = {"master": master, "parent_model": parent, "terminals": {}}
            records[device] = current
            continue
        match = re.match(r"^\s+(\S+)\s+:\s+(\S+)\s*$", line)
        if match and current is not None:
            terminal, node = match.groups()
            if terminal in current["terminals"]:
                raise ValueError("duplicate primitive terminal: " + terminal)
            current["terminals"][terminal] = node
    if not records:
        raise ValueError("no terminal-to-node evidence")
    return records


def physical_noise_port(device, mechanism, terminal_map, available_traces):
    """Resolve supported independent primitive sources; never merge ports.

    Intrinsic channel noise is between the *intrinsic* d/s nodes, whereas
    access resistance is between external and intrinsic terminals. Diode
    shot noise / correlated gate noise are deliberately not called thermal.
    """
    row = terminal_map.get(device)
    if row is None:
        raise ValueError("missing terminal evidence: " + device)
    master, terminals = row["master"].lower(), row["terminals"]
    available = set(available_traces)
    if master == "resistor" and mechanism == "rn":
        positive, negative, units = terminals["1"], terminals["2"], "V/A"
    elif master == "inductor" and mechanism == "rn":
        branch = device + ":1"
        if branch not in available:
            raise ValueError("missing inductor series-voltage transfer: " + branch)
        return {"kind": "series_voltage", "trace": branch, "transfer_units": "V/V",
                "source_units": "V^2/Hz", "master": master,
                "unit_contract": "spectre18_inductor_series_rn_v1"}
    elif master in ("bsim3v3", "bsim4") and mechanism in ("id", "rd", "rs"):
        internal_d, internal_s = device + ":int_d", device + ":int_s"
        # Spectre eliminates a zero access resistance and its internal node.
        internal_d = internal_d if internal_d in available else terminals["d"]
        internal_s = internal_s if internal_s in available else terminals["s"]
        positive, negative = {"id": (internal_d, internal_s),
                              "rd": (terminals["d"], internal_d),
                              "rs": (terminals["s"], internal_s)}[mechanism]
        units = "V/A"
    else:
        raise ValueError("unsupported thermal primitive/mechanism: " + master + "/" + mechanism)
    for node in (positive, negative):
        if node != "0" and node not in available:
            raise ValueError("missing node-current transfer: " + node)
    return {"kind": "parallel_current", "positive": positive, "negative": negative,
            "transfer_units": units, "source_units": "A^2/Hz", "master": master}


def port_transfer(values, port):
    if port["kind"] == "series_voltage":
        return values[port["trace"]]
    def node(name):
        return 0j if name == "0" else values[name]
    return node(port["positive"]) - node(port["negative"])
