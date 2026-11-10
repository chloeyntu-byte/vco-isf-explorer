"""Dependency-free local web server for the VCO ISF explorer UI."""

from __future__ import annotations

import argparse
import ipaddress
import json
import mimetypes
import os
import secrets
import threading
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .core.error_catalog import enrich_error
from .core.errors import (
    CoreError,
    JobFailedError,
    JobNotFoundError,
    JobNotReadyError,
    PreflightError,
    ResumeError,
    ValidationError,
)
from .core.service import ToolService


PACKAGE_ROOT = Path(__file__).resolve().parent
UI_ROOT = PACKAGE_ROOT / "ui"
DEFAULT_WORKSPACE = Path.home() / ".vco_isf_explorer"


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str, code: str = "ISF-E1001"):
        super().__init__(message)
        self.status = status
        self.code = code

    def to_dict(self) -> dict[str, Any]:
        return enrich_error(
            {
                "code": self.code,
                "kind": "api_error",
                "message": str(self),
                "details": {},
            }
        )


class AppState:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.api_token = secrets.token_urlsafe(32)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.service = ToolService(self.workspace)


class Handler(BaseHTTPRequestHandler):
    server_version = "VcoIsfExplorer/1.0"

    @property
    def app(self) -> AppState:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        print("[ui] " + fmt % args)

    def _json(self, status: int, value: Any) -> None:
        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _core_status(error: CoreError) -> int:
        if isinstance(error, JobNotFoundError):
            return HTTPStatus.NOT_FOUND
        if isinstance(
            error, (JobNotReadyError, JobFailedError, ResumeError)
        ):
            return HTTPStatus.CONFLICT
        if isinstance(error, (ValidationError, PreflightError)):
            return HTTPStatus.BAD_REQUEST
        return HTTPStatus.UNPROCESSABLE_ENTITY

    def _send_exception(self, error: Exception) -> None:
        if isinstance(error, ApiError):
            self._json(error.status, {"ok": False, "error": error.to_dict()})
            return
        if isinstance(error, CoreError):
            self._json(
                self._core_status(error),
                {"ok": False, "error": error.to_dict()},
            )
            return
        payload = enrich_error(
            {
                "code": "ISF-E9000",
                "kind": "internal_error",
                "message": str(error),
                "details": {},
            }
        )
        self._json(
            HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": payload}
        )

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid Content-Length") from exc
        if length < 0:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid Content-Length")
        if length > 8 * 1024 * 1024:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request exceeds 8 MiB")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw or b"{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid JSON body") from exc
        if not isinstance(value, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "JSON body must be an object")
        return value

    def _guard_local_request(self, *, check_origin: bool = False) -> None:
        if not _is_allowed_host_header(self.headers.get("Host")):
            raise ApiError(
                HTTPStatus.FORBIDDEN,
                "request Host must be literal localhost or IPv4 loopback",
            )
        origin = self.headers.get("Origin")
        if check_origin and origin is not None and not _is_same_origin(
            origin, self.headers.get("Host")
        ):
            raise ApiError(
                HTTPStatus.FORBIDDEN,
                "request Origin must match this Web UI origin",
            )

    def _guard_api_token(self) -> None:
        provided = self.headers.get("X-ISF-Token")
        expected = self.app.api_token
        if not isinstance(provided, str) or not secrets.compare_digest(
            provided, expected
        ):
            raise ApiError(
                HTTPStatus.FORBIDDEN,
                "missing or invalid local API token",
            )

    def _static(self, request_path: str) -> None:
        relative = request_path.lstrip("/") or "index.html"
        candidate = (UI_ROOT / relative).resolve()
        try:
            candidate.relative_to(UI_ROOT.resolve())
        except ValueError as exc:
            raise ApiError(HTTPStatus.FORBIDDEN, "invalid static path") from exc
        if not candidate.is_file():
            candidate = UI_ROOT / "index.html"
        data = candidate.read_bytes()
        mime = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime + ("; charset=utf-8" if mime.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _file(self, query: dict[str, list[str]]) -> None:
        raw = query.get("path", [""])[0]
        if not raw:
            raise ApiError(HTTPStatus.BAD_REQUEST, "path is required")
        path = Path(raw).expanduser().resolve()
        if not self.app.service.path_is_allowed(path):
            raise ApiError(HTTPStatus.FORBIDDEN, "path is outside configured roots")
        if not path.is_file():
            raise ApiError(HTTPStatus.NOT_FOUND, "file not found")
        data = path.read_bytes()
        mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._guard_local_request()
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if path.startswith("/api/"):
                self._guard_api_token()
            if path == "/api/health":
                self._json(HTTPStatus.OK, self.app.service.health())
            elif path == "/api/errors":
                self._json(HTTPStatus.OK, self.app.service.error_catalog())
            elif path == "/api/config":
                self._json(HTTPStatus.OK, self.app.service.load_config())
            elif path == "/api/states":
                self._json(HTTPStatus.OK, self.app.service.list_states())
            elif path.startswith("/api/states/"):
                self._json(HTTPStatus.OK, self.app.service.load_state({"id": urllib.parse.unquote(path[len("/api/states/"):])}))
            elif path == "/api/jobs":
                self._json(HTTPStatus.OK, self.app.service.list_jobs())
            elif path == "/api/file":
                self._file(urllib.parse.parse_qs(parsed.query))
            elif path.startswith("/api/jobs/"):
                self._json(HTTPStatus.OK, self.app.service.get_job(path.split("/")[-1]))
            elif path.startswith("/api/results/"):
                self._json(HTTPStatus.OK, self.app.service.get_result(path.split("/")[-1]))
            elif path.startswith("/api/"):
                raise ApiError(HTTPStatus.NOT_FOUND, "unknown API endpoint")
            else:
                self._static(parsed.path)
        except (ApiError, CoreError) as exc:
            self._send_exception(exc)
        except (FileNotFoundError, KeyError) as exc:
            self._send_exception(
                ApiError(HTTPStatus.NOT_FOUND, str(exc), code="ISF-E6001")
            )
        except (ValueError, UnicodeError) as exc:
            self._send_exception(ApiError(HTTPStatus.BAD_REQUEST, str(exc)))
        except Exception as exc:  # keep the UI responsive and preserve diagnosis
            self._send_exception(exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._guard_local_request(check_origin=True)
            self._guard_api_token()
            path = urllib.parse.urlparse(self.path).path.rstrip("/")
            payload = self._body()
            if path == "/api/config":
                result = self.app.service.save_config(payload)
            elif path == "/api/states":
                result = self.app.service.save_state(payload)
            elif path == "/api/preflight":
                result = self.app.service.preflight(payload)
            elif path == "/api/offsets":
                result = self.app.service.preview_offsets(payload)
            elif path == "/api/discover":
                result = self.app.service.discover(payload)
            elif path == "/api/maestro/discover":
                result = self.app.service.discover_maestro(payload)
            elif path == "/api/maestro/pair":
                result = self.app.service.inspect_maestro_pair(payload)
            elif path == "/api/import":
                result = self.app.service.import_as_job(payload)
            elif path == "/api/run":
                result = {"ok": True, "job_id": self.app.service.start_run(payload)}
            elif path.startswith("/api/jobs/") and path.endswith("/resume"):
                parts = path.split("/")
                result = self.app.service.resume_job(parts[-2], payload)
            elif path.startswith("/api/jobs/") and path.endswith("/stop-after-offset"):
                result = self.app.service.stop_after_current_offset(path.split("/")[-2])
            else:
                raise ApiError(HTTPStatus.NOT_FOUND, "unknown API endpoint")
            self._json(HTTPStatus.OK, result)
        except (ApiError, CoreError) as exc:
            self._send_exception(exc)
        except (ValueError, FileNotFoundError, KeyError) as exc:
            self._send_exception(
                ApiError(HTTPStatus.BAD_REQUEST, str(exc), code="ISF-E1001")
            )
        except Exception as exc:
            self._send_exception(exc)


def _is_loopback_host(host: str) -> bool:
    if str(host).lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(str(host))
        return address.version == 4 and address.is_loopback
    except ValueError:
        return False


def _is_allowed_host_header(value: str | None) -> bool:
    """Accept only an unambiguous literal loopback HTTP Host value."""

    if not isinstance(value, str) or not value or value != value.strip():
        return False
    if any(character.isspace() for character in value):
        return False
    if any(character in value for character in "/\\@?#,[]"):
        return False
    if value.count(":") > 1:
        return False
    host, separator, port = value.partition(":")
    if separator:
        if not port.isascii() or not port.isdigit():
            return False
        try:
            if not 1 <= int(port, 10) <= 65535:
                return False
        except ValueError:
            return False
    return _is_loopback_host(host)


def _is_loopback_origin(value: str | None) -> bool:
    """Validate a browser Origin without DNS resolution or IPv6 ambiguity."""

    if not isinstance(value, str) or not value or value != value.strip():
        return False
    if any(character.isspace() for character in value):
        return False
    if any(character in value for character in "\\@,[]"):
        return False
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme.lower() not in ("http", "https"):
        return False
    if not parsed.netloc or parsed.username is not None or parsed.password is not None:
        return False
    if parsed.path or parsed.query or parsed.fragment:
        return False
    if parsed.netloc.count(":") > 1:
        return False
    if port is not None and not 1 <= port <= 65535:
        return False
    return _is_allowed_host_header(parsed.netloc)


def _is_same_origin(value: str | None, host: str | None) -> bool:
    """Require browser writes to originate from this exact HTTP listener."""

    if not _is_loopback_origin(value) or not _is_allowed_host_header(host):
        return False
    try:
        parsed = urllib.parse.urlsplit(str(value))
    except ValueError:
        return False
    return (
        parsed.scheme.lower() == "http"
        and parsed.netloc.lower() == str(host).lower()
    )


def build_server(host: str, port: int, workspace: Path) -> ThreadingHTTPServer:
    if not _is_loopback_host(host):
        raise ValueError(
            "the compatibility Web UI may bind only to localhost/loopback"
        )
    server = ThreadingHTTPServer((host, port), Handler)
    if not _is_loopback_host(str(server.server_address[0])):
        server.server_close()
        raise ValueError("the resolved Web UI bind address is not IPv4 loopback")
    server.app = AppState(workspace)  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="VCO ISF explorer compatibility Web UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--open", action="store_true", help="open the UI in the default browser")
    args = parser.parse_args(argv)
    try:
        server = build_server(args.host, args.port, args.workspace)
    except ValueError as exc:
        parser.error(str(exc))
    base_url = f"http://{args.host}:{server.server_address[1]}/"
    url = base_url + "#token=" + urllib.parse.quote(
        server.app.api_token, safe=""  # type: ignore[attr-defined]
    )
    print(f"VCO ISF Explorer: {url}")
    print(f"Workspace: {args.workspace.expanduser().resolve()}")
    if args.open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
