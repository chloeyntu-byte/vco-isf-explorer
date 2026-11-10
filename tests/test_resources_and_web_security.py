from __future__ import annotations

import unittest
import tempfile
import threading
from http.client import HTTPConnection
from pathlib import Path

from isf_tool import resources
from isf_tool.server import (
    _is_allowed_host_header,
    _is_loopback_host,
    _is_loopback_origin,
    _is_same_origin,
    build_server,
)


class RuntimeResourceTests(unittest.TestCase):
    def test_source_runtime_scripts_and_synthetic_configs_exist(self):
        for name in (
            "cadence_dual_tb_runner.py",
            "dynamic_isf_closure_engine.py",
            "export_audited_bundle.py",
            "extract_dual_tb_results.py",
        ):
            self.assertTrue(resources.script_path(name).is_file(), name)
        self.assertTrue(
            resources.config_path("engine_pair_example_flat.json").is_file()
        )

    def test_unknown_runtime_script_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown bundled script"):
            resources.script_path("not-bundled.py")


class WebBindingSecurityTests(unittest.TestCase):
    def test_only_literal_loopback_hosts_are_allowed(self):
        for host in ("localhost", "127.0.0.1", "127.20.30.40"):
            with self.subTest(host=host):
                self.assertTrue(_is_loopback_host(host))
        for host in ("0.0.0.0", "::", "::1", "example.test", "192.0.2.1", ""):
            with self.subTest(host=host):
                self.assertFalse(_is_loopback_host(host))

    def test_non_loopback_is_rejected_before_binding(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            build_server("0.0.0.0", 0, Path("unused"))

    def test_request_host_accepts_only_unambiguous_ipv4_loopback(self):
        for value in (
            "localhost",
            "LOCALHOST:8765",
            "127.0.0.1",
            "127.20.30.40:443",
        ):
            with self.subTest(value=value):
                self.assertTrue(_is_allowed_host_header(value))
        for value in (
            None,
            "",
            " localhost",
            "localhost ",
            "local host",
            "localhost/path",
            "localhost\\path",
            "user@localhost",
            "localhost:0",
            "localhost:65536",
            "localhost:http",
            "[::1]",
            "::1",
            "example.test",
            "127.0.0.1.example",
            "127.0.0.1/evil",
        ):
            with self.subTest(value=value):
                self.assertFalse(_is_allowed_host_header(value))

    def test_post_origin_accepts_only_http_loopback_origin(self):
        for value in (
            "http://localhost",
            "https://LOCALHOST:443",
            "http://127.0.0.1:8765",
            "https://127.99.2.3",
        ):
            with self.subTest(value=value):
                self.assertTrue(_is_loopback_origin(value))
        for value in (
            None,
            "",
            "null",
            "ftp://localhost",
            "http://example.test",
            "http://[::1]",
            "http://user@localhost",
            "http://localhost/",
            "http://localhost?query=1",
            "http://localhost#fragment",
            " http://localhost",
            "http://localhost ",
            "http://localhost:0",
            "http://localhost:65536",
            "http://localhost\\evil",
        ):
            with self.subTest(value=value):
                self.assertFalse(_is_loopback_origin(value))

    def test_write_origin_must_match_exact_http_listener(self):
        self.assertTrue(
            _is_same_origin("http://127.0.0.1:8765", "127.0.0.1:8765")
        )
        self.assertFalse(
            _is_same_origin("http://127.0.0.2:8765", "127.0.0.1:8765")
        )
        self.assertFalse(
            _is_same_origin("https://127.0.0.1:8765", "127.0.0.1:8765")
        )

    def test_ipv4_loopback_server_rechecks_actual_bound_address(self):
        with tempfile.TemporaryDirectory() as temporary:
            server = build_server("127.0.0.1", 0, Path(temporary))
            try:
                self.assertTrue(_is_loopback_host(server.server_address[0]))
            finally:
                server.server_close()

    def test_handler_enforces_host_and_post_origin_guards(self):
        with tempfile.TemporaryDirectory() as temporary:
            server = build_server("127.0.0.1", 0, Path(temporary))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]

            def request(method, path, *, host, origin=None, token=None):
                connection = HTTPConnection("127.0.0.1", port, timeout=3)
                headers = {"Host": host}
                body = None
                if method == "POST":
                    headers["Content-Type"] = "application/json"
                    body = b"{}"
                if origin is not None:
                    headers["Origin"] = origin
                if token is not None:
                    headers["X-ISF-Token"] = token
                connection.request(method, path, body=body, headers=headers)
                response = connection.getresponse()
                status = response.status
                response.read()
                connection.close()
                return status

            try:
                local_host = "127.0.0.1:%d" % port
                token = server.app.api_token
                self.assertGreaterEqual(len(token), 32)
                self.assertEqual(request("GET", "/", host=local_host), 200)
                self.assertEqual(
                    request("GET", "/api/health", host=local_host), 403
                )
                self.assertEqual(
                    request(
                        "GET", "/api/health", host=local_host, token="incorrect"
                    ),
                    403,
                )
                self.assertEqual(
                    request(
                        "GET", "/api/health", host=local_host, token=token
                    ),
                    200,
                )
                self.assertEqual(
                    request(
                        "GET", "/api/health", host="evil.test", token=token
                    ),
                    403,
                )
                self.assertEqual(
                    request(
                        "POST",
                        "/api/not-found",
                        host=local_host,
                        origin="https://evil.test",
                        token=token,
                    ),
                    403,
                )
                self.assertEqual(
                    request(
                        "POST",
                        "/api/not-found",
                        host=local_host,
                        origin="http://127.0.0.2:%d" % port,
                        token=token,
                    ),
                    403,
                )
                self.assertEqual(
                    request(
                        "POST", "/api/not-found", host=local_host, token=token
                    ),
                    404,
                )
            finally:
                server.shutdown()
                thread.join(timeout=3)
                server.server_close()


if __name__ == "__main__":
    unittest.main()
