"""Read-only typed PSF noise spectra; no positional fields or unit guessing."""

from __future__ import annotations

import json
import math
import re


_TOKEN = re.compile(r'\s*("(?:\\.|[^"\\])*"|[()]|[^\s()]+)')


class _Reader:
    def __init__(self, text):
        self.tokens = [("string", json.loads(v)) if v.startswith('"') else ("atom", v)
                       for v in _TOKEN.findall(text)]
        self.index = 0

    def more(self):
        return self.index < len(self.tokens)

    def peek(self):
        return self.tokens[self.index][1] if self.more() else None

    def pop(self, expected=None):
        if not self.more():
            raise ValueError("unexpected end of PSF section")
        value = self.tokens[self.index][1]
        self.index += 1
        if expected is not None and value != expected:
            raise ValueError("expected PSF token %r, got %r" % (expected, value))
        return value

    def value(self):
        if self.peek() == "(":
            self.pop()
            values = []
            while self.peek() != ")":
                values.append(self.value())
            self.pop(")")
            return values
        kind, value = self.tokens[self.index] if self.more() else (None, None)
        self.pop()
        if kind == "string":
            return value
        try:
            number = float(value)
        except (ValueError, TypeError):
            return value
        if not math.isfinite(number):
            raise ValueError("non-finite PSF value")
        return number

    def properties(self):
        props = {}
        if self.peek() == "PROP":
            self.pop()
            self.pop("(")
            while self.peek() != ")":
                key = self.pop()
                if key in props:
                    raise ValueError("duplicate PSF property: " + key)
                props[key] = self.value()
            self.pop(")")
        return props


def _descriptor(reader):
    kind = reader.pop()
    if kind == "STRUCT":
        reader.pop("(")
        fields = []
        while reader.peek() != ")":
            name = reader.pop()
            if any(field["name"] == name for field in fields):
                raise ValueError("duplicate PSF struct field: " + name)
            fields.append(dict(_descriptor(reader), name=name))
        reader.pop(")")
        result = {"kind": "struct", "fields": fields}
    elif kind in ("FLOAT", "COMPLEX"):
        numeric = reader.pop()
        if (kind == "FLOAT" and numeric not in ("DOUBLE", "COMPLEX")) or (kind == "COMPLEX" and numeric != "DOUBLE"):
            raise ValueError("unsupported PSF numeric type: " + numeric)
        result = {"kind": "scalar", "numeric_type": "COMPLEX" if kind == "COMPLEX" else numeric}
    else:
        raise ValueError("unsupported PSF type: " + str(kind))
    props = reader.properties()
    result.update(properties=props, units=props.get("units"), master=props.get("master"))
    return result


def _typed_value(value, descriptor, label):
    if descriptor["kind"] == "struct":
        fields = descriptor["fields"]
        if not isinstance(value, list) or len(value) != len(fields):
            raise ValueError("PSF struct/schema shape mismatch: " + label)
        return {field["name"]: _typed_value(v, field, label + "." + field["name"])
                for field, v in zip(fields, value)}
    if descriptor["numeric_type"] == "COMPLEX":
        if not isinstance(value, list) or len(value) != 2 or not all(isinstance(v, (int, float)) for v in value):
            raise ValueError("invalid complex PSF value: " + label)
        return complex(*value)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError("invalid real PSF value: " + label)
    return float(value)


def parse_psf_ascii(text):
    """Return header, typed fields/masters, trace declarations and samples.

    Preserves the simulator-declared units, including primitive-specific
    source-unit conventions. A caller must validate physical source topology;
    the parser never silently changes an inductor's declared A units to V.
    """
    if not isinstance(text, str):
        raise ValueError("PSF dump must be text")
    sections = {}
    current = None
    for line in text.splitlines():
        if line.strip() in ("HEADER", "TYPE", "SWEEP", "TRACE", "VALUE", "END"):
            current = line.strip()
            if current in sections:
                raise ValueError("duplicate PSF section: " + current)
            sections[current] = []
        elif current:
            sections[current].append(line)
    if not {"TYPE", "TRACE", "VALUE", "END"}.issubset(sections):
        raise ValueError("PSF dump requires TYPE, TRACE, VALUE and terminal END")
    readers = {key: _Reader("\n".join(lines)) for key, lines in sections.items()}
    header = {}
    reader = readers.get("HEADER", _Reader(""))
    while reader.more():
        key = reader.pop()
        if key in header:
            raise ValueError("duplicate PSF header: " + key)
        header[key] = reader.value()
    types = {}
    reader = readers["TYPE"]
    while reader.more():
        name = reader.pop()
        if name in types:
            raise ValueError("duplicate PSF type: " + name)
        types[name] = _descriptor(reader)
    traces, trace_properties = {}, {}
    reader = readers["TRACE"]
    while reader.more():
        name, type_name = reader.pop(), reader.pop()
        if name in traces or type_name not in types:
            raise ValueError("duplicate trace or unknown trace type: " + name)
        traces[name] = type_name
        trace_properties[name] = reader.properties()
    samples = []
    current_sample = None
    reader = readers["VALUE"]
    while reader.more():
        name = reader.pop()
        value = reader.value()
        if name in ("freq", "relative frequency", "frequency"):
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("invalid PSF frequency coordinate")
            current_sample = {"frequency": float(value), "axis": name, "values": {}}
            samples.append(current_sample)
        else:
            if current_sample is None or name not in traces or name in current_sample["values"]:
                raise ValueError("unbound, duplicate or unknown PSF trace value: " + name)
            current_sample["values"][name] = _typed_value(value, types[traces[name]], name)
    if not samples:
        raise ValueError("PSF dump has no frequency samples")
    return {"header": header, "types": types, "traces": traces,
            "trace_properties": trace_properties, "samples": samples}


def _finite(value, label, *, nonnegative=False):
    if isinstance(value, bool):
        raise ValueError(label + " must be finite real")
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(label + " must be finite real") from exc
    if not math.isfinite(value) or (nonnegative and value < 0):
        raise ValueError(label + " must be finite non-negative real")
    return value


def noise_sample(dataset, frequency, *, abs_tol_hz=0.0, relative_tol=1e-12):
    """Require one exact-with-serialization-tolerance frequency, never nearest."""
    frequency = _finite(frequency, "frequency")
    absolute = _finite(abs_tol_hz, "absolute tolerance", nonnegative=True)
    relative = _finite(relative_tol, "relative tolerance", nonnegative=True)
    matches = [row for row in dataset["samples"] if abs(row["frequency"] - frequency) <= max(absolute, relative * abs(frequency))]
    if len(matches) != 1:
        raise ValueError("frequency %.16g has %d exact PSF matches" % (frequency, len(matches)))
    return matches[0]


def quantity_as_psd(value, units, expected_domain=None):
    """Convert explicitly declared A/V ASD or PSD, without guessing units."""
    definitions = {"A^2/Hz": ("A", False), "A/sqrt(Hz)": ("A", True),
                   "V^2/Hz": ("V", False), "V/sqrt(Hz)": ("V", True)}
    if units not in definitions:
        raise ValueError("unsupported or missing noise units: " + str(units))
    domain, is_asd = definitions[units]
    if expected_domain is not None and expected_domain != domain:
        raise ValueError("noise domain mismatch: expected " + str(expected_domain) + ", got " + domain)
    value = _finite(value, "noise value", nonnegative=True)
    psd = value * value if is_asd else value
    if not math.isfinite(psd):
        raise ValueError("noise PSD overflow")
    return {"domain": domain, "psd": psd, "asd": value if is_asd else math.sqrt(value),
            "psd_units": domain + "^2/Hz", "asd_units": domain + "/sqrt(Hz)",
            "raw_quantity": "ASD" if is_asd else "PSD"}


def noise_quantity(dataset, frequency, trace, field=None, *, expected_domain=None):
    """Read one named scalar/struct field using its own TYPE units."""
    sample = noise_sample(dataset, frequency)
    if trace not in dataset["traces"] or trace not in sample["values"]:
        raise ValueError("missing requested noise trace: " + trace)
    descriptor = dataset["types"][dataset["traces"][trace]]
    value = sample["values"][trace]
    if field is not None:
        if descriptor["kind"] != "struct" or field not in value:
            raise ValueError("missing requested noise mechanism: " + str(field))
        descriptor = next(item for item in descriptor["fields"] if item["name"] == field)
        value = value[field]
    elif descriptor["kind"] != "scalar":
        raise ValueError("a structured noise trace requires a field name")
    result = quantity_as_psd(value, descriptor["units"], expected_domain)
    result.update(trace=trace, field=field, frequency=sample["frequency"])
    return result


def classify_mechanism(master, mechanism):
    """Conservative primitive classification, not name-only thermal guessing."""
    primitive = str(master or "").lower()
    if mechanism == "fn":
        return "flicker"
    if mechanism in ("total", "totalsrc"):
        return "aggregate"
    mos = primitive in ("b3v3", "bsim3", "bsim4", "bsimcmg")
    if mechanism == "id" and mos:
        return "channel_thermal"
    if mechanism == "id" and primitive == "diode":
        return "shot"
    if mechanism in ("rd", "rs", "rg", "rb") and mos:
        return "resistive_thermal"
    if mechanism == "rn" and primitive in ("resistor", "inductor"):
        return "resistive_thermal"
    return "unclassified"


def contributor_records(dataset, frequency, *, include_totals=False):
    """Named mechanisms, source/output PSDs and gains with raw master/units.

    .src and .out can have different units. .xf2 is never a noise PSD.
    """
    sample = noise_sample(dataset, frequency)
    records = []
    for device, type_name in dataset["traces"].items():
        descriptor = dataset["types"][type_name]
        if descriptor["kind"] != "struct":
            continue
        if device not in sample["values"]:
            raise ValueError("missing noise contributor sample: " + device)
        for field in descriptor["fields"]:
            name = field["name"]
            mechanism, component = name, "contribution"
            if name.rsplit(".", 1)[-1] in ("src", "out", "xf2"):
                mechanism, component = name.rsplit(".", 1)
            if name in ("total", "totalsrc"):
                mechanism, component = "total", "source_total" if name == "totalsrc" else "output_total"
                if not include_totals:
                    continue
            value = sample["values"][device][name]
            record = {"device": device, "master": descriptor["master"], "type_name": type_name,
                      "field": name, "mechanism": mechanism, "component": component,
                      "units": field["units"], "value": value, "frequency": sample["frequency"],
                      "noise_family": classify_mechanism(descriptor["master"], mechanism)}
            if component == "xf2":
                if not re.fullmatch(r'\([AV]\^2/Hz\)/\([AV]\^2/Hz\)', str(field["units"])):
                    raise ValueError("unrecognized power-gain units: " + str(field["units"]))
                record.update(quantity_kind="power_gain", power_gain=_finite(value, "power gain", nonnegative=True))
            else:
                converted = quantity_as_psd(value, field["units"])
                record.update(converted, quantity_kind="noise")
                record["psd_" + converted["domain"] + "2_Hz"] = converted["psd"]
            records.append(record)
    return records
