"""Shared plumbing for the web UI tests: fixture adapters only, no real platform is ever contacted."""

from __future__ import annotations

import re
import time

from starlette.testclient import TestClient

from sociyx import paths
from sociyx.adapters.fixture import DemoFixtureAdapter
from sociyx.adapters.instagram import InstagramAdapter
from sociyx.store import Store
from sociyx.tui.demo import seed_demo
from sociyx.web.app import create_app
from sociyx.web.state import WebState

PORT = 18431
ORIGIN = f"http://127.0.0.1:{PORT}"
SECRET = "launch-secret-for-tests-0123456789abcdef"
NOSLEEP = lambda s: None  # noqa: E731


class Env:
    """A WebState + app + signed-in client over the demo fixture accounts."""

    def __init__(self, faults=None, seed=True, signed_in=True, page_size=7):
        self.faults = faults if faults is not None else {}
        self.page_size = page_size
        if seed:
            seed_demo(Store(paths.db_path()))
        self.state = WebState(
            port=PORT, demo=True, launch_secret=SECRET, adapter_factory=self.factory,
            executor_options={"sleep": NOSLEEP, "min_interval": 0, "verify_delay": 0},
            scan_options={"sleep": NOSLEEP})
        self.app = create_app(self.state)
        self.client = TestClient(self.app, base_url=ORIGIN, follow_redirects=False)
        self.csrf = ""
        if signed_in:
            self.sign_in()

    def factory(self, platform, **options):
        if platform == "instagram":
            return InstagramAdapter()
        return DemoFixtureAdapter(faults=self.faults, page_size=self.page_size)

    def sign_in(self):
        r = self.client.get(f"/?launch={SECRET}")
        assert r.status_code == 303, r.text
        home = self.client.get("/")
        assert home.status_code == 200
        self.csrf = re.search(r'name="csrf-token" content="([^"]+)"', home.text).group(1)
        return home

    # ---- requests ----

    def get(self, path, htmx=False, **kw):
        headers = {"HX-Request": "true"} if htmx else {}
        return self.client.get(path, headers=headers, **kw)

    def post(self, path, data=None, htmx=True, **kw):
        headers = {"Origin": ORIGIN, "X-CSRF-Token": self.csrf}
        if htmx:
            headers["HX-Request"] = "true"
        headers.update(kw.pop("headers", {}))
        return self.client.post(path, data=data or {}, headers=headers, **kw)

    # ---- helpers ----

    @staticmethod
    def key(account_id):
        from sociyx.web.app import b64

        return b64(account_id)

    def base(self, account_id="fixture:alice", platform="fixture"):
        return f"/a/{platform}/{self.key(account_id)}"

    def wait(self, cond, timeout=15.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            got = cond()
            if got:
                return got
            time.sleep(0.03)
        raise AssertionError("condition not reached in time")

    def wait_run(self, plan_id):
        def done():
            r = self.get(f"/run/{plan_id}/status")
            return r if "hx-redirect" in r.headers or "Stopped unexpectedly" in r.text or "Not started" in r.text else None

        return self.wait(done)

    def wait_run_stopped(self, plan_id):
        self.wait(lambda: not self.state.runs[plan_id].running)
        return self.state.runs[plan_id].snapshot()


def ids_of(html: str) -> list[str]:
    return re.findall(r'hx-vals=\'\{"item_id": "([^"]+)"\}\'', html)
