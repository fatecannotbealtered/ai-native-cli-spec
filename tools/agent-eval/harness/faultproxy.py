"""Fault proxy for the VER-006 lost-reply scenario (standard library only).

The proxy sits between the tool and the upstream and forwards every request. When armed, it lets the first
request that matches its rule (by default any method but GET, HEAD and OPTIONS; optionally a method list and a
path regex) reach the upstream, reads the upstream's whole answer and, if that answer is a 2xx, drops it: it
closes the client connection without replying, so the write is applied upstream while the tool never learns
its outcome. That happens exactly once; an armed request the upstream refuses (not 2xx) is passed through and
the proxy stays armed. Every request is logged, without headers or bodies, to a JSON file that is rewritten
atomically after each event, so the grader can see that the dropped write reached the upstream and what
followed it.

    python harness/faultproxy.py --upstream http://localhost:8929 [--listen 127.0.0.1:0]
        [--arm [--arm-method POST[,PUT]] [--arm-path REGEX]] [--log FILE] [--stop-file FILE] [--max-seconds N]

It prints the URL to give the tool, then serves until Ctrl+C, SIGTERM, the stop file appearing or --max-seconds.
lane.py starts one per lost-reply trial with spawn() and stops it with halt(); run.py runs FaultProxy in process.
"""

from __future__ import annotations

import argparse
import datetime as dt
import http.client
import json
import os
import re
import signal
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOP_BY_HOP = {"connection", "keep-alive", "proxy-connection", "proxy-authenticate", "proxy-authorization", "te",
              "trailer", "trailers", "transfer-encoding", "upgrade"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
REWRITTEN = {"location", "content-location", "link"}  # response headers that may carry the upstream's address
MAX_BODY = 64 << 20
MAX_LOGGED = 2000  # requests kept in the log
UPSTREAM_TIMEOUT = 120.0
IDLE_TIMEOUT = 120.0


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Rule:
    """Which request the armed proxy lets through and then drops the reply of."""

    def __init__(self, method=None, path: str | None = None):
        if isinstance(method, str):
            method = [m for m in method.split(",") if m.strip()]
        self.methods = frozenset(m.strip().upper() for m in method) if method else None
        self.path = path or None
        self._rx = re.compile(path) if path else None

    @classmethod
    def from_spec(cls, spec: dict | None) -> "Rule":
        """From tasks.LOST_REPLY: {"method": "POST" or ["POST", "PUT"], "path": regex}, both optional."""
        spec = spec or {}
        unknown = set(spec) - {"method", "path"}
        if unknown:
            raise ValueError(f"unknown fault rule key(s): {', '.join(sorted(unknown))}")
        return cls(spec.get("method"), spec.get("path"))

    def matches(self, method: str, path: str) -> bool:
        m = method.upper()
        method_ok = m in self.methods if self.methods else m not in SAFE_METHODS
        return method_ok and (self._rx is None or self._rx.search(path) is not None)

    def describe(self) -> dict:
        return {"method": sorted(self.methods) if self.methods else f"any but {', '.join(sorted(SAFE_METHODS))}",
                "path": self.path}


def write_json_atomic(path: Path, data: dict) -> None:
    """Replace path with data; retried, because on Windows a reader holding the file blocks the rename."""
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    for attempt in range(50):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 49:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.02)


def read_log(path: Path) -> dict | None:
    """The proxy's log, or None when there is none yet (or it is being replaced)."""
    for _ in range(20):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (PermissionError, ValueError):
            time.sleep(0.02)
    return None


def summary(log: dict | None) -> dict | None:
    """What a result records about the proxy: did it drop a reply, what the upstream said to that write, and how
    many requests matching the rule were forwarded after the drop (a re-sent write shows up there)."""
    if not log:
        return None
    dropped = log.get("dropped")
    after = [r for r in log.get("requests", []) if dropped and r.get("matched") and r["seq"] > dropped["seq"]
             and r.get("action") == "forwarded"]
    return {"armed": log.get("arm") is not None, "dropped": bool(dropped),
            "dropped_request": {k: dropped.get(k) for k in ("method", "path", "upstream_status")} if dropped else None,
            "matched_after_drop": len(after), "requests": log.get("request_count", 0)}


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False  # FaultProxy.stop() closes the client connections and joins the handlers itself
    allow_reuse_address = os.name != "nt"  # on Windows it would let a second server bind a port in use

    def __init__(self, address, handler, proxy: "FaultProxy"):
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        self.proxy = proxy
        super().__init__(address, handler)

    def server_bind(self):
        # HTTPServer.server_bind also resolves the host's FQDN, which can stall on reverse DNS; nothing uses it
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def handle_error(self, request, client_address):
        # a client that hung up, or a connection stop() or a drop cut, is not an error worth a traceback
        if not isinstance(sys.exc_info()[1], OSError):
            super().handle_error(request, client_address)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive
    timeout = IDLE_TIMEOUT
    server: _Server

    def setup(self):
        super().setup()
        self.server.proxy._track(self.connection, add=True)

    def finish(self):
        try:
            super().finish()
        finally:
            self.server.proxy._track(self.connection, add=False)

    def log_message(self, format, *args):  # noqa: A002 - the JSON log records every request
        pass

    def do_GET(self):
        self.server.proxy._handle(self)

    do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_GET

    # ------------------------------------------------------------ request bodies

    def read_body(self) -> bytes:
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            return self._read_chunked()
        lengths = self.headers.get_all("Content-Length") or []
        if not lengths:
            return b""
        if len({v.strip() for v in lengths}) != 1 or not lengths[0].strip().isdigit():
            raise ValueError("invalid Content-Length")
        n = int(lengths[0])
        if n > MAX_BODY:
            raise ValueError("request body too large")
        data = self.rfile.read(n)
        if len(data) != n:
            raise ValueError("connection closed inside the request body")
        return data

    def _read_chunked(self) -> bytes:
        body = bytearray()
        while True:
            line = self.rfile.readline(65537)
            if not line.endswith(b"\n"):
                raise ValueError("connection closed inside a chunked body")
            try:
                size = int(line.split(b";", 1)[0].strip(), 16)
            except ValueError:
                raise ValueError("invalid chunk size") from None
            if size == 0:
                while True:  # trailers, up to the empty line
                    t = self.rfile.readline(65537)
                    if t in (b"\r\n", b"\n", b""):
                        return bytes(body)
            if len(body) + size > MAX_BODY:
                raise ValueError("request body too large")
            data = self.rfile.read(size)
            if len(data) != size or self.rfile.readline(65537) not in (b"\r\n", b"\n"):
                raise ValueError("malformed chunk")
            body += data

    def reply(self, status: int, reason: str, headers: list, body: bytes) -> None:
        self.send_response_only(status, reason)
        no_body = self.command == "HEAD" or status in (204, 304) or 100 <= status < 200
        for k, v in headers:
            lk = k.lower()
            if lk in HOP_BY_HOP or (lk == "content-length" and not (self.command == "HEAD" or status == 304)):
                continue
            if lk in REWRITTEN:
                v = self.server.proxy.rewrite(v)
            self.send_header(k, v)
        if not no_body:
            self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if not no_body:
            self.wfile.write(body)
        self.wfile.flush()

    def drop(self) -> None:
        """Close the connection without a byte of reply."""
        self.close_connection = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class FaultProxy:
    """The proxy, run in process: FaultProxy(upstream, rule).start() ... stop(); also a context manager."""

    def __init__(self, upstream: str, rule: Rule | dict | None = None, listen: str = "127.0.0.1:0",
                 log_path: Path | None = None):
        u = urllib.parse.urlsplit(upstream)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError(f"upstream must be an http or https URL, not {upstream!r}")
        self.upstream = upstream.rstrip("/")
        self._u = u
        self._upstream_origin = f"{u.scheme}://{u.netloc}"
        self.rule = rule if isinstance(rule, Rule) or rule is None else Rule.from_spec(rule)
        self.log_path = Path(log_path) if log_path else None
        host, _, port = listen.rpartition(":")
        host = host.strip("[]") or "127.0.0.1"
        if not port.isdigit():
            raise ValueError(f"listen must be HOST:PORT, not {listen!r}")
        self._listen = (host, int(port))
        self._lock = threading.Lock()
        self._armed = self.rule is not None
        self._in_flight = False
        self._conns: dict = {}
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._seq = 0
        self._stopped = False
        self.log: dict = {}

    # ------------------------------------------------------------ lifecycle

    @property
    def url(self) -> str:
        """Where the proxy listens: http://host:port."""
        host, port = self._server.server_address[:2]
        return f"http://{'[' + host + ']' if ':' in host else host}:{port}"

    @property
    def tool_url(self) -> str:
        """What to give the tool instead of the upstream URL: the proxy, with the upstream's base path."""
        return self.url + self._u.path.rstrip("/")

    def start(self) -> "FaultProxy":
        self._server = _Server(self._listen, _Handler, self)
        self.log = {"upstream": self.upstream, "listen": self.url, "tool_url": self.tool_url, "pid": os.getpid(),
                    "arm": self.rule.describe() if self.rule else None, "armed": self._armed,
                    "started_at": now_utc(), "stopped_at": None, "dropped": None, "request_count": 0,
                    "requests": []}
        self._save()
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.1},
                                        name="faultproxy", daemon=True)
        self._thread.start()
        return self

    def stop(self, join_timeout: float = 5.0) -> dict:
        """Stop serving, close every client connection, wait briefly for the handlers, write the final log."""
        if self._server is None or self._stopped:
            return self.log
        self._server.shutdown()
        with self._lock:
            conns = list(self._conns.items())
        for sock, _ in conns:
            try:
                sock.shutdown(socket.SHUT_RDWR)  # wakes a handler waiting on an idle keep-alive connection (POSIX)
                if os.name == "nt":
                    # Windows wakes it only when the handle closes; socket.close() would wait for the handler's
                    # makefile() objects, so close the handle itself
                    super(socket.socket, sock).close()
            except OSError:
                pass
        self._server.server_close()
        deadline = time.monotonic() + join_timeout
        for _, t in conns:
            if t is not threading.current_thread():
                t.join(max(0.0, deadline - time.monotonic()))
        if self._thread:
            self._thread.join(max(0.0, deadline - time.monotonic()))
        with self._lock:
            self._stopped = True
            self.log["stopped_at"] = now_utc()
            self._save_locked()
        return self.log

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    def _track(self, sock, add: bool) -> None:
        with self._lock:
            if add:
                self._conns[sock] = threading.current_thread()
            else:
                self._conns.pop(sock, None)

    def _save(self) -> None:
        with self._lock:
            self._save_locked()

    def _save_locked(self) -> None:
        if self.log_path:
            try:
                write_json_atomic(self.log_path, self.log)
            except OSError as e:  # a log that cannot be written must not break the proxying
                print(f"faultproxy: cannot write {self.log_path}: {e}", file=sys.stderr)

    # ------------------------------------------------------------ requests

    def rewrite(self, value: str) -> str:
        """Point the upstream's own address in Location and Link headers back at the proxy."""
        return value.replace(self._upstream_origin, self.url)

    def _claim(self, method: str, path: str) -> tuple[bool, bool]:
        """(matched, claimed): only one matching request at a time may be the one whose reply is dropped."""
        if self.rule is None or not self.rule.matches(method, path):
            return False, False
        with self._lock:
            if self._armed and not self._in_flight:
                self._in_flight = True
                return True, True
        return True, False

    def _settle(self, consumed: bool) -> None:
        with self._lock:
            self._in_flight = False
            if consumed:
                self._armed = False
                self.log["armed"] = False

    def _record(self, entry: dict, dropped: bool = False) -> None:
        with self._lock:
            if self._stopped:
                return
            self._seq += 1
            entry = {"seq": self._seq, **entry}
            self.log["request_count"] = self._seq
            if dropped:
                self.log["dropped"] = entry
            reqs = self.log["requests"]
            reqs.append(entry)
            if len(reqs) > MAX_LOGGED:
                del reqs[0]
            self._save_locked()

    def _forward(self, method: str, target: str, headers: list, body: bytes):
        u = self._u
        if u.scheme == "https":
            conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=UPSTREAM_TIMEOUT,
                                               context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=UPSTREAM_TIMEOUT)
        try:
            conn.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
            for k, v in headers:
                conn.putheader(k, v)
            if body or method.upper() in ("POST", "PUT", "PATCH"):
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body or None)
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, resp.reason, resp.getheaders(), data
        finally:
            conn.close()

    def _handle(self, h: _Handler) -> None:
        started = time.monotonic()
        target = h.path
        if "://" in target:  # absolute form, as sent to a forward proxy
            p = urllib.parse.urlsplit(target)
            target = urllib.parse.urlunsplit(("", "", p.path or "/", p.query, ""))
        path = target.split("?", 1)[0]
        entry = {"at": now_utc(), "method": h.command, "path": path}
        try:
            body = h.read_body()
        except ValueError as e:
            h.close_connection = True
            h.reply(400, "Bad Request", [("Content-Type", "text/plain")], f"fault proxy: {e}\n".encode())
            self._record({**entry, "action": "bad_request", "status": 400, "matched": False})
            return
        matched, claimed = self._claim(h.command, path)
        conn_tokens = {t.strip().lower() for t in h.headers.get("Connection", "").split(",")}
        headers = [(k, v) for k, v in h.headers.items()
                   if k.lower() not in HOP_BY_HOP | conn_tokens | {"host", "content-length", "expect"}]
        headers.insert(0, ("Host", self._u.netloc))
        try:
            status, reason, resp_headers, data = self._forward(h.command, target, headers, body)
        except (OSError, http.client.HTTPException) as e:
            if claimed:
                self._settle(consumed=False)
            h.close_connection = True
            h.reply(502, "Bad Gateway", [("Content-Type", "text/plain")], b"fault proxy: upstream unreachable\n")
            self._record({**entry, "action": "upstream_error", "status": 502, "matched": matched,
                          "error": type(e).__name__, "request_bytes": len(body)})
            return
        entry.update(request_bytes=len(body), response_bytes=len(data), matched=matched,
                     ms=round((time.monotonic() - started) * 1000))
        if claimed and 200 <= status < 300:
            self._settle(consumed=True)
            self._record({**entry, "action": "dropped", "upstream_status": status}, dropped=True)
            h.drop()
            return
        if claimed:
            self._settle(consumed=False)
        self._record({**entry, "action": "forwarded", "status": status})  # logged before the client has it
        try:
            h.reply(status, reason, resp_headers, data)
        except OSError:
            h.close_connection = True


# ---------------------------------------------------------------- a detached proxy per trial (lane.py)

CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def spawn(upstream: str, rule: dict | None, log_path: Path, stop_path: Path, max_seconds: int = 7200,
          python: str | None = None, wait_s: float = 15.0) -> dict:
    """Start the proxy as a detached process that outlives the caller (a helper agent's Bash call) and return
    its log once it listens: log["tool_url"] is the address to give the tool. Stop it with halt()."""
    log_path, stop_path = Path(log_path), Path(stop_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.unlink(missing_ok=True)
    stop_path.unlink(missing_ok=True)
    cmd = [python or sys.executable, str(Path(__file__).resolve()), "--upstream", upstream, "--listen", "127.0.0.1:0",
           "--log", str(log_path), "--stop-file", str(stop_path), "--max-seconds", str(max_seconds)]
    if rule is not None:
        r = Rule.from_spec(rule)  # fail here, not in the child, on a bad rule
        cmd.append("--arm")
        if r.methods:
            cmd += ["--arm-method", ",".join(sorted(r.methods))]
        if r.path:
            cmd += ["--arm-path", r.path]
    err_path = log_path.with_name(log_path.name + ".stderr")
    with open(err_path, "wb") as err:
        kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": err, "close_fds": True}
        if os.name == "nt":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            try:  # leave the caller's job object, if it allows that, so the proxy survives the caller
                proc = subprocess.Popen(cmd, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **kw)
            except OSError:
                proc = subprocess.Popen(cmd, creationflags=flags, **kw)
        else:
            proc = subprocess.Popen(cmd, start_new_session=True, **kw)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        log = read_log(log_path)
        if log and log.get("listen") and not log.get("stopped_at"):
            return log
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    if proc.poll() is None:
        proc.kill()
    raise RuntimeError(f"fault proxy did not start: {err_path.read_text(encoding='utf-8', errors='replace')[-500:]}")


def halt(log_path: Path, stop_path: Path, timeout: float = 10.0) -> dict | None:
    """Stop a proxy started with spawn(): create its stop file, wait for its final log, kill it if it hangs."""
    log = read_log(log_path)
    if not log or log.get("stopped_at"):
        return log
    Path(stop_path).write_text("stop\n", encoding="utf-8")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        log = read_log(log_path) or log
        if log.get("stopped_at"):
            return log
        time.sleep(0.05)
    try:
        os.kill(int(log["pid"]), signal.SIGTERM)
    except (OSError, KeyError, ValueError, TypeError):
        return log
    for _ in range(40):  # POSIX: SIGTERM stops it cleanly; Windows: terminated, the log stays as last written
        time.sleep(0.05)
        log = read_log(log_path) or log
        if log.get("stopped_at"):
            break
    return log


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--upstream", required=True, help="base URL of the upstream, e.g. http://localhost:8929")
    ap.add_argument("--listen", default="127.0.0.1:0", help="HOST:PORT to listen on (port 0: any free port)")
    ap.add_argument("--arm", action="store_true", help="drop the reply to the first matching write, once")
    ap.add_argument("--arm-method", help="methods the rule matches, comma-separated (default: any but GET, HEAD, "
                                         "OPTIONS)")
    ap.add_argument("--arm-path", help="regex searched in the request path (default: any path)")
    ap.add_argument("--log", help="JSON log file, rewritten after every request")
    ap.add_argument("--stop-file", help="stop once this file exists")
    ap.add_argument("--max-seconds", type=float, default=0, help="stop after this long (0: no limit)")
    a = ap.parse_args(argv)
    if (a.arm_method or a.arm_path) and not a.arm:
        ap.error("--arm-method and --arm-path need --arm")
    try:
        rule = Rule(a.arm_method, a.arm_path) if a.arm else None
        proxy = FaultProxy(a.upstream, rule, a.listen, Path(a.log) if a.log else None).start()
    except (ValueError, re.error, OSError) as e:
        print(f"faultproxy: {e}", file=sys.stderr)
        return 2
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except (OSError, ValueError):
            pass
    print(f"{proxy.tool_url} -> {proxy.upstream}; " + (f"armed: {json.dumps(rule.describe())}" if rule else
                                                       "not armed"), flush=True)
    deadline = time.monotonic() + a.max_seconds if a.max_seconds > 0 else None
    try:
        while not stop.wait(0.2):
            if a.stop_file and os.path.exists(a.stop_file):
                break
            if deadline and time.monotonic() >= deadline:
                break
    except KeyboardInterrupt:
        pass
    proxy.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
