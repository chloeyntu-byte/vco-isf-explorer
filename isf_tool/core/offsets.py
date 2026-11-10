"""Frequency-offset inputs. This module never computes or interpolates noise."""
from __future__ import annotations

import copy
import math
import re
from collections.abc import Mapping

from .errors import ValidationError

MAX_OFFSET_POINTS = 256
_UNITS = {"": 1.0, "Hz": 1.0, "hz": 1.0, "k": 1e3, "K": 1e3,
          "kHz": 1e3, "khz": 1e3, "M": 1e6, "MHz": 1e6, "mhz": 1e6,
          "meg": 1e6, "G": 1e9, "GHz": 1e9, "ghz": 1e9}
_NUMBER = re.compile(r"^([+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*([A-Za-z]*)$")


def frequency_hz(value):
    """Parse a positive finite number; bare numbers are Hz, capital M is mega."""
    if isinstance(value, bool) or value is None:
        raise ValidationError("频偏必须是正数，例如 1k、10k、1M（裸数字单位 Hz）")
    match = _NUMBER.fullmatch(str(value).strip())
    if not match or match.group(2) not in _UNITS:
        raise ValidationError("频偏格式无效：%r；支持 Hz、k、M、G，不支持含糊单位 m" % value)
    hz = float(match.group(1)) * _UNITS[match.group(2)]
    if not math.isfinite(hz) or hz <= 0:
        raise ValidationError("频偏必须为有限正数：%r" % value)
    return hz


def _count(value, label):
    if isinstance(value, bool):
        raise ValidationError("%s 必须为正整数" % label)
    try:
        number = float(value)
        count = int(number)
    except (TypeError, ValueError, OverflowError):
        raise ValidationError("%s 必须为正整数" % label)
    if number != count or count <= 0 or count > MAX_OFFSET_POINTS:
        raise ValidationError("%s 必须为 1 至 %d 的整数" % (label, MAX_OFFSET_POINTS))
    return count


def _finish(values):
    values = sorted(set(frequency_hz(item) for item in values))
    if not values:
        raise ValidationError("至少需要一个频偏")
    if len(values) > MAX_OFFSET_POINTS:
        raise ValidationError("频偏最多 %d 点；请增加步长或减少间隔数" % MAX_OFFSET_POINTS)
    return values


def expand_offsets(analysis):
    """Expand legacy single, explicit list, linear or logarithmic sample sites."""
    if not isinstance(analysis, Mapping):
        raise ValidationError("analysis 必须为对象")
    spec = analysis.get("offsets") or {"mode": "single"}
    if not isinstance(spec, Mapping):
        raise ValidationError("analysis.offsets 必须为对象")
    mode = spec.get("mode", "single")
    if mode == "single":
        offset = analysis.get("offset") or {}
        if not isinstance(offset, Mapping):
            raise ValidationError("analysis.offset 必须为对象")
        value = offset.get("hz")
        if value is None:
            unit = offset.get("unit", "Hz")
            if unit not in _UNITS:
                raise ValidationError("频偏单位无效：%s" % unit)
            value = frequency_hz(offset.get("value")) * _UNITS[unit]
        return [frequency_hz(value)]
    if mode == "list":
        values = spec.get("values", "")
        if isinstance(values, str):
            # Whitespace separates entries, but allow an optional space before a unit.
            values = re.sub(r"(?<=\d)\s+(?=[kKMG]|[Hh]z)", "", values.strip())
            if re.search(r"[,，;]\s*[,，;]|^[,，;]|[,，;]$", values):
                raise ValidationError("频偏列表中有空项")
            values = re.split(r"[,，;\s]+", values) if values else []
        if not isinstance(values, (list, tuple)):
            raise ValidationError("多个单点需输入列表，例如 1k,10k,1M,100M")
        if len(values) > MAX_OFFSET_POINTS:
            raise ValidationError("频偏输入最多 %d 项" % MAX_OFFSET_POINTS)
        return _finish(values)
    if mode not in ("linear", "log"):
        raise ValidationError("频偏模式必须为 single、list、linear 或 log")
    start, stop = frequency_hz(spec.get("start")), frequency_hz(spec.get("stop"))
    if stop < start:
        raise ValidationError("终止频偏不能小于起始频偏")
    if mode == "linear":
        spacing = spec.get("spacing") or ("step" if spec.get("step") not in (None, "") else "intervals")
        if spacing == "intervals":
            intervals = _count(spec.get("intervals"), "间隔数")
            if intervals + 1 > MAX_OFFSET_POINTS:
                raise ValidationError("N 个间隔产生 N+1 点；最多 %d 点" % MAX_OFFSET_POINTS)
            return _finish([start + (stop - start) * i / intervals for i in range(intervals + 1)])
        if spacing != "step":
            raise ValidationError("线性间隔方式必须为 step 或 intervals")
        step = frequency_hz(spec.get("step"))
        ratio = (stop - start) / step
        if not math.isfinite(ratio) or ratio >= MAX_OFFSET_POINTS:
            raise ValidationError("步长产生的频偏点过多，最多 %d 点" % MAX_OFFSET_POINTS)
        count = int(math.floor(ratio + 1e-12))
        points = [start + i * step for i in range(count + 1)]
        if math.isclose(points[-1], stop, rel_tol=1e-12, abs_tol=0):
            points[-1] = stop
        else:
            points.append(stop)
        return _finish(points)
    per_decade = _count(spec.get("points_per_decade", 1), "每 decade 点数")
    span = math.log10(stop) - math.log10(start)
    if span * per_decade >= MAX_OFFSET_POINTS:
        raise ValidationError("对数频偏点过多，最多 %d 点" % MAX_OFFSET_POINTS)
    count = int(math.floor(span * per_decade + 1e-12))
    points = [10 ** (math.log10(start) + i / per_decade) for i in range(count + 1)]
    points[0] = start
    if math.isclose(points[-1], stop, rel_tol=1e-12, abs_tol=0):
        points[-1] = stop
    else:
        points.append(stop)
    return _finish(points)


def normalize_offset_analysis(analysis):
    result = copy.deepcopy(dict(analysis))
    values = expand_offsets(result)
    # Keep the user's input recipe; values_hz is derived and never authoritative.
    if "offsets" in result:
        result["offsets"] = copy.deepcopy(result["offsets"] or {"mode": "single"})
        result["offsets"]["values_hz"] = values
    if len(values) > 1 or (result.get("offsets") or {}).get("mode") != "single" and "offsets" in result:
        result["offset"] = {"value": values[0], "unit": "Hz", "hz": values[0]}
    else:
        result.setdefault("offset", {})["hz"] = values[0]
    return result


def offset_label(hz):
    return "%.12g Hz" % float(hz)


def offset_key(hz):
    return format(float(hz), ".17g")
