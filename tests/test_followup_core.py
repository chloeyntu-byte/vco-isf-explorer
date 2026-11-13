"""Offline follow-up regressions: finite bands, combined curves and state API.

All Spectre hooks are mocked. HTTP checks bind only an ephemeral loopback port
and use a temporary workspace; no installed EDA or user state is accessed.
"""

import copy
import json
import math
from http.client import HTTPConnection
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from isf_tool import thermal
from isf_tool.core import thermal_execution
from isf_tool.core.result_presentation import harmonic_noise_series
from isf_tool.core.thermal_budget import thermal_evidence
from isf_tool.server import Handler, build_server


def _budget(parts=None, *, offset=1000.0, limit=None):
    if parts is None:
        parts = {0: 1e-12, -1: 2e-12, 1: 3e-12}
    power = math.fsum(parts.values())
    value = dict(schema_version=1, status="available",
                 scope="gm_pair_and_lc_network", pair_model="symmetric_independent",
                 passive_scope="passive_only_lc_roots_v1",
                 noise_quantity="PM_SSB", pm_validation="independent_analytic_fixture",
                 carrier_peak_V=1.0, offset_Hz=offset, f0_Hz=5e9,
                 predicted_psd_V2_Hz=power,
                 sources=[dict(device="synthetic.R", mechanism="rn", psd_V2_Hz=power,
                               input_band_contributions=[dict(input_band=k, psd_V2_Hz=p)
                                                         for k, p in parts.items()])])
    if limit is not None:
        value.update(input_max_harmonic=limit, requested_max_harmonic=limit)
    return value


def _sample(identity="A", offset=1000.0, *, qs=None, parts=None, missing_thermal=False):
    qs = {0: 1e-6, 1: -1e-6} if qs is None else qs
    point = identity + "@" + str(offset)
    magnitude = abs(sum(qs.values()))
    row = dict(point=point, operating_point_id=identity, corner=identity,
               offset_Hz=offset, carrier_peak_V=1.0, f0_Hz=5e9,
               dynamic_pm_asd_V_rtHz=magnitude,
               predicted_pair_pm_asd_V_rtHz=math.sqrt(2) * magnitude,
               pair_model="symmetric_independent", numerical_quality_pass=False)
    if not missing_thermal:
        row["thermal_noise"] = _budget(parts, offset=offset)
    harmonics = [dict(k=k, Q_real_V_rtHz=complex(q).real, Q_imag_V_rtHz=complex(q).imag)
                 for k, q in qs.items()]
    return dict(summary=[row], points={point: dict(harmonics=harmonics)})


def _merge(*samples):
    return dict(summary=[copy.deepcopy(row) for sample in samples for row in sample["summary"]],
                points={key: copy.deepcopy(value) for sample in samples
                        for key, value in sample["points"].items()})


class ThermalFiniteBandFollowupTests(unittest.TestCase):
    def test_input_limit_matches_independent_fourier_oracle_and_reports_tail(self):
        # (2 + 2 cos t)(2 + cos t) = 5 + 6 cos t + cos 2t.
        # Independent m powers are 25, 9, 9, 1/4, 1/4, not |sum B_m|^2.
        h = {-1: 1, 0: 2, 1: 1}
        a = {-1: .5, 0: 2, 1: .5}
        before = copy.deepcopy((h, a))
        for limit, expected in ((0, 25), (1, 43), (2, 43.5), (9, 43.5)):
            with self.subTest(limit=limit):
                result = thermal.calculate_white_thermal(h, a, max_input_harmonic=limit)
                self.assertEqual(expected, result["total_psd_V2_Hz"])
                truncation = result["input_band_truncation"]
                self.assertEqual(43.5 - expected, truncation["omitted_psd_V2_Hz"])
                self.assertEqual(43.5, truncation["untruncated_psd_V2_Hz"])
                self.assertEqual(limit, truncation["max_harmonic"])
                self.assertTrue(all(abs(row["input_band"]) <= limit
                                    for row in result["input_band_contributions"]))
        self.assertEqual(before, (h, a))

    def test_limit_keeps_within_band_cancellation_and_zero_evidence(self):
        # DC convolution paths cancel, while two independent edge bands survive.
        result = thermal.calculate_white_thermal(
            {-1: .5, 0: 1, 1: .5}, {-1: -1, 0: 1, 1: -1}, max_input_harmonic=0)
        self.assertEqual(0.0, result["total_psd_V2_Hz"])
        self.assertEqual([0], [row["input_band"] for row in result["input_band_contributions"]])
        self.assertGreater(result["input_band_truncation"]["omitted_psd_V2_Hz"], 0)

    def test_invalid_or_conflicting_band_controls_fail(self):
        for limit in (-1, True, False, 1.5, "2", float("nan"), float("inf")):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                thermal.calculate_white_thermal({0: 1}, {0: 1}, max_input_harmonic=limit)
        with self.assertRaisesRegex(ValueError, "exclusive"):
            thermal.calculate_white_thermal({0: 1}, {0: 1},
                                            max_input_harmonic=1, input_bands=[0])

    def test_budget_limit_gate_preserves_legacy_and_accepts_exact_boundary(self):
        legacy = _budget({-2: 2e-12, 2: 3e-12})
        self.assertAlmostEqual(5e-12, thermal_evidence(dict(thermal_noise=legacy))["predicted_psd_V2_Hz"], delta=1e-25)
        current = _budget({-2: 2e-12, 2: 3e-12}, limit=2)
        self.assertEqual(2, thermal_evidence(dict(thermal_noise=current))["input_max_harmonic"])
        zero = _budget({0: 0}, limit=0)
        parsed = thermal_evidence(dict(thermal_noise=zero))
        self.assertEqual(0, parsed["predicted_psd_V2_Hz"])
        self.assertIsNone(parsed["phase_noise_dBc_Hz"])

    def test_budget_rejects_out_of_range_input_even_when_its_power_is_zero(self):
        for k in (-2, 2):
            with self.subTest(k=k):
                evidence = _budget({0: 1e-12, k: 0}, limit=1)
                with self.assertRaisesRegex(ValueError, "exceeds"):
                    thermal_evidence(dict(thermal_noise=evidence))

    def test_budget_rejects_invalid_limit_and_mismatched_requested_order(self):
        for limit in (-1, True, False, "1", 1.5):
            evidence = _budget(limit=1)
            evidence["input_max_harmonic"] = limit
            with self.subTest(limit=limit), self.assertRaisesRegex(ValueError, "limit"):
                thermal_evidence(dict(thermal_noise=evidence))
        for requested in (None, 0, 2, "1"):
            evidence = _budget(limit=1)
            evidence["requested_max_harmonic"] = requested
            with self.subTest(requested=requested), self.assertRaisesRegex(ValueError, "order"):
                thermal_evidence(dict(thermal_noise=evidence))

    def test_private_measurement_uses_requested_order_not_reference_sideband(self):
        class StopBeforeSimulation(Exception):
            pass

        for limit in (0, 1, 3):
            with self.subTest(limit=limit), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "main_vco" / "netlist"
                source.mkdir(parents=True)
                original = "R1 (a b) resistor r=7\nhb (a b) hb maxharms=[20]\n"
                (source / "input.scs").write_text(original)
                engine = mock.Mock()
                engine.main_output.return_value = dict(negative="a", positive="b")
                engine.exact_hb_fundamental.return_value = 5e9
                engine._copy_regular_tree.side_effect = lambda src, dst: dst.mkdir(parents=True)
                engine.run_spectre.side_effect = StopBeforeSimulation
                cfg = dict(offset_hz=1000, max_harmonic=limit, main_max_sideband=17)
                with self.assertRaises(StopBeforeSimulation):
                    thermal_execution.run_thermal_point(engine, cfg, {}, root)
                generated = (root / "thermal" / "main" / "netlist" / "input.scs").read_text()
                self.assertEqual(2, generated.count("maxsideband=%d " % (limit + 1)))
                self.assertNotIn("maxsideband=18 ", generated)
                self.assertEqual(original, (source / "input.scs").read_text())
                self.assertIn("R1 (a b) resistor r=7", generated)

    def test_insufficient_saved_hb_support_fails_instead_of_clamping_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "main_vco" / "netlist"
            source.mkdir(parents=True)
            (source / "input.scs").write_text("hb (a b) hb maxharms=[2]\n")
            engine = mock.Mock()
            engine.main_output.return_value = dict(negative="a", positive="b")
            engine.exact_hb_fundamental.return_value = 5e9
            engine._copy_regular_tree.side_effect = lambda src, dst: dst.mkdir(parents=True)
            with mock.patch.object(thermal_execution, "_read", return_value={"header": {"harms": 2}}):
                with self.assertRaisesRegex(ValueError, "do not support requested"):
                    thermal_execution.run_thermal_point(
                        engine, dict(offset_hz=1000, max_harmonic=3), {}, root)
            self.assertFalse((root / "thermal" / "budget.json").exists())


class CombinedHarmonicFollowupTests(unittest.TestCase):
    def test_bands_put_coherent_flicker_in_baseband_only(self):
        fixture = _sample()
        before = copy.deepcopy(fixture)
        curves = harmonic_noise_series(fixture, "A", scope="pair", noise_kind="both")
        self.assertEqual(["input_band_0", "input_band_1", "calculated_total", "hbnoise_total"],
                         [row["id"] for row in curves])
        for curve, power in zip(curves, (1e-12, 5e-12, 6e-12)):
            self.assertAlmostEqual(10 * math.log10(2 * power), curve["points"][0][1])
            self.assertEqual("pair", curve["scope"])
            self.assertIs(curve["samples"][0]["quality"]["numerical_quality_pass"], False)
        self.assertNotAlmostEqual(curves[-2]["points"][0][1], 10 * math.log10(2 * 10e-12))
        self.assertEqual(before, fixture)

    def test_measured_zero_branch_is_not_missing_branch(self):
        for qs, parts, expected in (
            ({0: 0j}, {0: 3e-12}, 3e-12),
            ({0: 1e-6j}, {0: 0}, 2e-12),
            ({0: 0j}, {0: 0}, 0),
        ):
            with self.subTest(qs=qs, parts=parts):
                curves = harmonic_noise_series(_sample(qs=qs, parts=parts), "A", scope="pair", noise_kind="both")
                self.assertEqual(3, len(curves))
                for curve in curves[:-1]:
                    if expected:
                        self.assertAlmostEqual(10 * math.log10(2 * expected), curve["points"][0][1])
                    else:
                        self.assertIsNone(curve["points"][0][1])
                json.dumps(curves, allow_nan=False)

    def test_missing_thermal_or_flicker_order_remains_gap_without_interpolation(self):
        first = _sample(offset=1000)
        absent_thermal = _sample(offset=10000, missing_thermal=True)
        absent_flicker_order = _sample(offset=100000)
        point = absent_flicker_order["summary"][0]["point"]
        absent_flicker_order["points"][point]["harmonics"].pop()
        absent_band = _sample(offset=1000000, parts={0: 1e-12})
        curves = harmonic_noise_series(_merge(first, absent_thermal, absent_flicker_order, absent_band),
                                       "A", scope="pair", noise_kind="both")
        self.assertEqual([1000, 10000, 100000, 1000000], [x for x, _ in curves[0]["points"]])
        self.assertIsNone(curves[0]["points"][1][1])
        self.assertEqual([None, -110.0, None], [point[1] for point in curves[1]["points"][1:]])
        self.assertIsNone(curves[-2]["points"][1][1])
        self.assertIsNotNone(curves[-2]["points"][2][1])

    def test_bands_union_input_bands_independently_of_flicker_pair_orders(self):
        curves = harmonic_noise_series(_sample(qs={0: 1e-6}, parts={-1: 2e-12, 1: 3e-12}),
                                       "A", scope="pair", noise_kind="both")
        self.assertEqual(["input_band_0","input_band_1","calculated_total","hbnoise_total"], [row["id"] for row in curves])
        self.assertAlmostEqual(10 * math.log10(14e-12), curves[-2]["points"][0][1])

    def test_identical_offsets_in_other_corners_cannot_supply_missing_values(self):
        fixture = _merge(_sample("B", 1000, qs={0: 99e-6}, parts={0: 99e-12}),
                         _sample("A", 100000, missing_thermal=True), _sample("A", 1000))
        curves = harmonic_noise_series(fixture, "A", scope="pair", noise_kind="both")
        self.assertEqual([1000, 100000], [x for x, _ in curves[-1]["points"]])
        self.assertAlmostEqual(10 * math.log10(12e-12), curves[-2]["points"][0][1])
        self.assertIsNone(curves[-2]["points"][1][1])
        self.assertTrue(all(sample["operating_point_id"] == "A" for curve in curves for sample in curve["samples"]))
        self.assertEqual([], harmonic_noise_series(fixture, "unknown", scope="pair", noise_kind="both"))


class StateApiFollowupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        self.log_patch = mock.patch.object(Handler, "log_message")
        self.log_patch.start()
        self.addCleanup(self.log_patch.stop)
        self.server = build_server("127.0.0.1", 0, self.workspace)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown)
        self.host = "127.0.0.1:%d" % self.server.server_address[1]
        self.token = self.server.app.api_token
        self.payload = dict(name="independent-api-fixture", config=dict(
            execution=dict(allow_execute=True),
            testbenches=dict(vco=dict(binding_fingerprint="Stale", history_fingerprint="Stale",
                                     mapping_overrides=dict(output_p="/arbitrary")))),
            view=dict(noise_kind="both", job_id="no-auto-run", cumulative_k=2))

    def _shutdown(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()

    def request(self, method, path, *, payload=None, raw=None, token=True, host=None, origin=None):
        headers = {"Host": host or self.host}
        if token is not False:
            headers["X-ISF-Token"] = self.token if token is True else token
        if origin is not None:
            headers["Origin"] = origin
        if method == "POST":
            headers["Content-Type"] = "application/json"
            raw = json.dumps(payload if payload is not None else {}).encode() if raw is None else raw
        connection = HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=3)
        try:
            connection.request(method, path, body=raw, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read()), dict(response.getheaders())
        finally:
            connection.close()

    def test_get_and_post_require_token_before_state_access(self):
        for method, path in (("GET", "/api/states"),
                             ("GET", "/api/states/20260921-120000-0123456789ab.json"),
                             ("POST", "/api/states")):
            for token in (False, "wrong-token"):
                with self.subTest(method=method, token=token):
                    status, body, _ = self.request(method, path, payload=self.payload, token=token)
                    self.assertEqual(403, status)
                    self.assertFalse(body["ok"])
        self.assertFalse((self.workspace / "states").exists())

    def test_post_origin_and_host_guards_apply_to_new_endpoint(self):
        origins = ("https://evil.test", "null", "http://127.0.0.2:%d" % self.server.server_address[1],
                   "https://" + self.host)
        for origin in origins:
            with self.subTest(origin=origin):
                self.assertEqual(403, self.request("POST", "/api/states", payload=self.payload, origin=origin)[0])
        self.assertEqual(403, self.request("GET", "/api/states", host="evil.test")[0])
        self.assertFalse((self.workspace / "states").exists())
        self.assertEqual(200, self.request("POST", "/api/states", payload=self.payload, origin="http://" + self.host)[0])

    def test_http_roundtrip_never_runs_or_restores_execution_authority(self):
        service = self.server.app.service
        before = service.load_config()
        with mock.patch.object(service, "start_run", side_effect=AssertionError("must not run")), \
             mock.patch.object(service, "preflight", side_effect=AssertionError("must not probe")):
            status, saved, headers = self.request("POST", "/api/states", payload=self.payload)
            self.assertEqual(200, status)
            self.assertEqual("no-store", headers["Cache-Control"])
            self.assertEqual(self.workspace / "states", Path(saved["path"]).parent)
            status, listing, _ = self.request("GET", "/api/states")
            self.assertEqual(200, status)
            self.assertEqual([saved["id"]], [row["id"] for row in listing["states"]])
            status, loaded, headers = self.request("GET", "/api/states/" + saved["id"])
            self.assertEqual(200, status)
            self.assertEqual("no-store", headers["Cache-Control"])
        config = loaded["state"]["config"]
        self.assertFalse(config["execution"]["allow_execute"])
        self.assertEqual("", config["testbenches"]["vco"]["binding_fingerprint"])
        self.assertEqual("", config["testbenches"]["vco"]["history_fingerprint"])
        self.assertEqual({"output_p": "/arbitrary"}, config["testbenches"]["vco"]["mapping_overrides"])
        self.assertEqual(self.payload["view"], loaded["state"]["view"])
        self.assertEqual(before, service.load_config())
        self.assertFalse((self.workspace / "config.json").exists())

    def test_encoded_traversal_and_symlink_cannot_read_outside_state_directory(self):
        for suffix in ("..%2Fconfig.json", "%2Fetc%2Fpasswd", "%252e%252e%252fsecret.json", "bad.json"):
            with self.subTest(suffix=suffix):
                status, body, _ = self.request("GET", "/api/states/" + suffix)
                self.assertEqual(400, status)
                self.assertFalse(body["ok"])
        _, saved, _ = self.request("POST", "/api/states", payload=self.payload)
        path = Path(saved["path"])
        content = path.read_bytes()
        path.unlink()
        outside = self.workspace / "not-a-state.json"
        outside.write_bytes(content)
        path.symlink_to(outside)
        self.assertEqual(400, self.request("GET", "/api/states/" + saved["id"])[0])
        _, listing, _ = self.request("GET", "/api/states")
        self.assertEqual([], listing["states"])
        self.assertEqual(1, len(listing["warnings"]))
        self.assertEqual(content, outside.read_bytes())

    def test_invalid_json_schema_missing_state_and_corruption_are_structured_errors(self):
        for raw in (b"{", b"[]", b"null"):
            with self.subTest(raw=raw):
                status, body, _ = self.request("POST", "/api/states", raw=raw)
                self.assertEqual(400, status)
                self.assertFalse(body["ok"])
                self.assertIn("error", body)
        for payload in (dict(self.payload, path="/tmp/escape.json"),
                        dict(self.payload, config=[]),
                        dict(self.payload, view={"unexpected": 1})):
            self.assertEqual(400, self.request("POST", "/api/states", payload=payload)[0])
        status, body, _ = self.request("GET", "/api/states/20260921-120000-0123456789ab.json")
        self.assertEqual(404, status)
        self.assertFalse(body["ok"])
        _, saved, _ = self.request("POST", "/api/states", payload=self.payload)
        Path(saved["path"]).write_bytes(b"\xffnot utf8")
        self.assertEqual(400, self.request("GET", "/api/states/" + saved["id"])[0])
        status, listing, _ = self.request("GET", "/api/states")
        self.assertEqual(200, status)
        self.assertEqual([], listing["states"])
        self.assertEqual(1, len(listing["warnings"]))


if __name__ == "__main__":
    unittest.main()
