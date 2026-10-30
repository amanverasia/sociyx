"""Background jobs of the web UI. Same threading rules as the TUI workers: every job thread opens its own
Store and its own adapter instance (SQLite connections and adapter clients must not cross threads) and
publishes progress only through plain attributes guarded by a lock, which request handlers read."""

from __future__ import annotations

import collections
import secrets
import threading
import time
from typing import Any

from ..executor import Executor, PlanRefused
from ..models import Account, ActionBlocked, AuthRequired, Outcome
from ..redact import scrub
from ..scanning import scan_account
from ..service import summary_line
from .state import WebState


class ScanJob:
    def __init__(self, state: WebState, account: Account, types: list[str] | None, fresh: bool, max_pages: int | None):
        self.state, self.account, self.types, self.fresh, self.max_pages = state, account, types, fresh, max_pages
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.running = True
        self.progress = "Starting..."
        self.lines: list[str] = []
        self.error = ""
        self.stopped = False
        self.thread = threading.Thread(target=self._work, name="sociyx-web-scan", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        with self.lock:
            if self.running:
                self.progress = "Stopping after the current page..."

    def _on_progress(self, ctype: str, pages: int, n: int) -> None:
        with self.lock:
            self.progress = f"{ctype}: {pages} pages, {n} items found"

    def _work(self) -> None:
        store = self.state.new_store()
        adapter = self.state.new_adapter(self.account.platform)
        try:
            opts = {"sleep": time.sleep, **self.state.scan_options}
            done = scan_account(store, adapter, self.account, self.types, fresh=self.fresh, max_pages=self.max_pages,
                                progress=self._on_progress, should_stop=self.stop_event.is_set, **opts)
            lines = [summary_line(s) for s in done]
            stopped = any(s.stopped for s in done) or self.stop_event.is_set()
            with self.lock:
                self.lines, self.stopped = lines, stopped
        except ActionBlocked as e:
            self._fail(f"The platform is restricting the account. {e.guidance()}")
        except AuthRequired as e:
            self._fail(f"Login required ({e}). Use 'Log in again' on the account page.")
        except Exception as e:  # noqa: BLE001
            self._fail(f"{type(e).__name__}: {e}")
        finally:
            adapter.close()
            store.close()
            with self.lock:
                self.running = False

    def _fail(self, msg: str) -> None:
        with self.lock:
            self.error = scrub(msg)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {"running": self.running, "progress": self.progress, "lines": list(self.lines),
                    "error": self.error, "stopped": self.stopped}


class RunJob:
    """The executor in a thread. The plan's confirmation token is read from the database *inside* this
    thread and handed straight to Executor.run; it never reaches a template or a response."""

    def __init__(self, state: WebState, plan_id: int, job_id: int, platform: str):
        self.state, self.plan_id, self.job_id, self.platform = state, plan_id, job_id, platform
        self.lock = threading.Lock()
        self.running = True
        self.kind = ""  # result | refused | error
        self.payload = ""
        self.state_text = "Starting..."
        self.deadline = 0.0
        self.reason = ""
        self.log: collections.deque[str] = collections.deque(maxlen=100)
        self.pause_requested = False
        self.thread = threading.Thread(target=self._work, name="sociyx-web-run", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _work(self) -> None:
        store = self.state.new_store()
        adapter = self.state.new_adapter(self.platform)

        def progress(item_id: str, outcome: Outcome, detail: str) -> None:
            with self.lock:
                self.log.append(f"{item_id}  {outcome.value}" + (f"  {scrub(detail)}" if detail else ""))
            hook = self.state.progress_hook
            if hook:
                hook(item_id, outcome, detail)

        def on_wait(seconds: float, reason: str) -> None:
            with self.lock:
                self.deadline, self.reason = time.monotonic() + seconds, reason

        def on_state(text: str) -> None:
            with self.lock:
                self.state_text = scrub(text)

        try:
            plan = store.get_plan(self.plan_id)
            ex = Executor(store, adapter, progress=progress, **{"on_wait": on_wait, "on_state": on_state,
                                                                **self.state.executor_options})
            result = ex.run(self.plan_id, plan.confirmation_token)
            self._finish("result", result.status)
        except PlanRefused as e:
            self._finish("refused", scrub(str(e)))
        except (Exception, KeyboardInterrupt) as e:  # noqa: BLE001
            self._finish("error", scrub(f"{type(e).__name__}: {e}"))
        finally:
            adapter.close()
            store.close()

    def _finish(self, kind: str, payload: str) -> None:
        with self.lock:
            self.kind, self.payload, self.running = kind, payload, False

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            left = self.deadline - time.monotonic()
            return {"running": self.running, "kind": self.kind, "payload": self.payload,
                    "state_text": self.state_text, "log": list(self.log),
                    "wait_left": left if self.running and left > 0 else 0.0, "reason": self.reason}


class LoginJob:
    """A platform login in a background thread, on the machine running the server (visible browser or OAuth
    loopback). A password, when given, lives only in this thread's closure for the duration of the call."""

    def __init__(self, state: WebState, platform: str, method: str | None, hint: str | None,
                 password: str = "", code: str = ""):
        self.state, self.platform, self.method, self.hint = state, platform, method, hint
        self.token = secrets.token_urlsafe(16)
        self.lock = threading.Lock()
        self.running = True
        self.ok = False
        self.message = ""
        self.account_label = ""
        self.account_ref: tuple[str, str] | None = None
        self._secrets = {"password": password, "code": code}
        self._calls = 0
        self.thread = threading.Thread(target=self._work, name="sociyx-web-login", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _prompt_secret(self, _prompt: str) -> str:
        """The adapter asks for the password first and a 2FA code second; answer from the form, never a terminal."""
        self._calls += 1
        value = self._secrets.get("password" if self._calls == 1 else "code", "")
        if not value:
            raise AuthRequired("Instagram asks for a verification (2FA) code. Log in again and fill in the code field.")
        return value

    @staticmethod
    def _prompt_text(_prompt: str) -> str:
        from ..models import PermanentError

        raise PermanentError("this login needs a value that was not provided in the form")

    def _work(self) -> None:
        adapter = None
        try:
            if self.platform == "fixture":
                adapter = self.state.new_adapter("fixture")
            else:
                adapter = self.state.new_adapter(self.platform, prompt_secret=self._prompt_secret,
                                                 prompt_text=self._prompt_text)
            acct = adapter.login(self.hint, self.method)
            store = self.state.new_store()
            try:
                store.upsert_account(acct)
            finally:
                store.close()
            with self.lock:
                self.ok, self.account_label = True, acct.handle or acct.account_id
                self.account_ref = (acct.platform, acct.account_id)
                self.message = f"Logged in as {self.account_label}."
        except AuthRequired as e:
            self._fail(f"Login was not completed: {e}. sociyx never bypasses a challenge or CAPTCHA; "
                       "finish it on the platform's own site or app, then try again.")
        except ActionBlocked as e:
            self._fail(f"The platform is restricting the account. {e.guidance()}")
        except Exception as e:  # noqa: BLE001
            self._fail(f"Login failed: {type(e).__name__}: {e}")
        finally:
            self._secrets.clear()
            if adapter is not None:
                try:
                    adapter.close()
                except Exception:  # noqa: BLE001
                    pass
            with self.lock:
                self.running = False

    def _fail(self, msg: str) -> None:
        with self.lock:
            self.message = scrub(msg)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {"running": self.running, "ok": self.ok, "message": self.message, "ref": self.account_ref}
