"""Minimal GitLab REST client for seeding, resetting and grading (stdlib only).

Worked example: when you port the harness, replace this with a client for your upstream.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from config import CFG


class GitLabError(Exception):
    def __init__(self, status: int, method: str, path: str, body: str):
        super().__init__(f"{method} {path} -> HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


def load_tokens() -> dict:
    """Per-user tokens minted by seed.py, the agent identity's (eval-bot) included."""
    return json.loads((CFG.secrets_dir / "tokens.json").read_text(encoding="utf-8"))


class GitLab:
    def __init__(self, token: str, host: str | None = None):
        self.token = token
        self.host = (host or CFG.upstream_url).rstrip("/")

    def request(self, method: str, path: str, data: dict | None = None, params: dict | None = None,
                ok_missing: bool = False, retries: int = 3):
        url = f"{self.host}/api/v4{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        body = None
        headers = {"PRIVATE-TOKEN": self.token}
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        for attempt in range(retries):
            req = urllib.request.Request(url, data=body, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "replace")
                if e.code == 404 and ok_missing:
                    return None
                if e.code in (502, 503) and attempt + 1 < retries:
                    time.sleep(3)
                    continue
                raise GitLabError(e.code, method, path, text) from None
            except urllib.error.URLError:
                if attempt + 1 < retries:
                    time.sleep(3)
                    continue
                raise

    def get(self, path, **params):
        return self.request("GET", path, params=params or None)

    def get_or_none(self, path, **params):
        return self.request("GET", path, params=params or None, ok_missing=True)

    def post(self, path, data=None):
        return self.request("POST", path, data=data or {})

    def put(self, path, data=None):
        return self.request("PUT", path, data=data or {})

    def delete(self, path, ok_missing=True):
        return self.request("DELETE", path, ok_missing=ok_missing)

    def all(self, path, **params):
        """Every item of a paginated list."""
        out, page = [], 1
        while True:
            batch = self.request("GET", path, params={**params, "per_page": 100, "page": page})
            out.extend(batch)
            if len(batch) < 100:
                return out
            page += 1


def pid(path_with_namespace: str) -> str:
    """URL-encoded project or group path for use in an API path."""
    return urllib.parse.quote(path_with_namespace, safe="")
