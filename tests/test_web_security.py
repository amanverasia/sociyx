"""Localhost threat model of the web UI: session, Host, Origin, CSRF, headers, logging, static files."""

import logging
import re

import pytest

from sociyx.web.security import COOKIE
from webhelpers import ORIGIN, PORT, SECRET, Env


@pytest.fixture
def env():
    return Env()


def all_headers(r):
    return {k.lower(): v for k, v in r.headers.items()}


# ---------------- session required ----------------


@pytest.mark.parametrize("path", ["/", "/help", "/login", "/import", "/static/app.js", "/results/1", "/run/1/status",
                                  "/plan/1/export"])
def test_no_cookie_is_401(path):
    e = Env(signed_in=False)
    r = e.client.get(path)
    assert r.status_code == 401
    assert "fixture" not in r.text.lower() or "Not signed in" in r.text


def test_post_without_session_is_401():
    e = Env(signed_in=False)
    r = e.client.post("/login/start", data={"platform": "fixture"}, headers={"Origin": ORIGIN})
    assert r.status_code == 401


def test_bogus_cookie_is_401():
    e = Env(signed_in=False)
    e.client.cookies.set(COOKIE, "not-a-session")
    assert e.client.get("/").status_code == 401


# ---------------- launch secret ----------------


def test_launch_secret_is_single_use_and_sets_strict_cookie():
    e = Env(signed_in=False)
    r = e.client.get(f"/?launch={SECRET}")
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}=")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie
    assert len(cookie.split("=", 1)[1].split(";")[0]) >= 40  # random session id
    assert SECRET not in cookie
    assert e.client.get("/").status_code == 200
    assert e.state.launch_secret is None
    # the same link again is dead, even from a fresh client
    other = Env.__new__(Env)
    again = e.client.__class__(e.app, base_url=ORIGIN, follow_redirects=False).get(f"/?launch={SECRET}")
    assert again.status_code == 403
    assert "set-cookie" not in again.headers


def test_wrong_secret_refused_and_does_not_burn_the_real_one():
    e = Env(signed_in=False)
    assert e.client.get("/?launch=wrong").status_code == 403
    assert e.client.get("/?launch=").status_code == 403
    assert e.client.get(f"/?launch={SECRET}").status_code == 303


def test_each_session_has_its_own_csrf_token():
    e = Env()
    assert len(e.csrf) >= 40
    assert e.csrf not in e.client.cookies.get(COOKIE)


# ---------------- Host / Origin / CSRF ----------------


@pytest.mark.parametrize("host", ["evil.example", "evil.example:%d" % PORT, "127.0.0.1", "localhost", "127.0.0.1:1",
                                  "127.0.0.1:%d.evil.com" % PORT, ""])
def test_wrong_host_is_refused(env, host):
    r = env.client.get("/", headers={"Host": host})
    assert r.status_code == 400
    r = env.client.get(f"/?launch={SECRET}", headers={"Host": host})
    assert r.status_code == 400


def test_localhost_host_is_accepted(env):
    r = env.client.get("/", headers={"Host": f"localhost:{PORT}"})
    assert r.status_code == 200


@pytest.mark.parametrize("origin", ["http://evil.example", "null", f"http://localhost:{PORT}", f"https://127.0.0.1:{PORT}",
                                    f"http://127.0.0.1:{PORT + 1}"])
def test_post_with_wrong_origin_is_403(env, origin):
    r = env.client.post("/import", data={"path": "/nonexistent"}, headers={"Origin": origin, "X-CSRF-Token": env.csrf})
    assert r.status_code == 403


def test_post_without_origin_or_referer_is_403(env):
    r = env.client.post("/import", data={"path": "/x"}, headers={"X-CSRF-Token": env.csrf})
    assert r.status_code == 403


def test_post_with_matching_referer_and_csrf_works(env):
    r = env.client.post("/import", data={"path": "/definitely/not/there"},
                        headers={"Referer": f"{ORIGIN}/import", "X-CSRF-Token": env.csrf})
    assert r.status_code == 422  # reached the handler: "does not exist"


def test_post_with_wrong_referer_is_403(env):
    r = env.client.post("/import", data={"path": "/x"}, headers={"Referer": "http://evil.example/x", "X-CSRF-Token": env.csrf})
    assert r.status_code == 403


def test_post_without_csrf_is_403(env):
    r = env.client.post("/import", data={"path": "/x"}, headers={"Origin": ORIGIN})
    assert r.status_code == 403


def test_post_with_wrong_csrf_is_403(env):
    r = env.client.post("/import", data={"path": "/x", "csrf_token": "nope"}, headers={"Origin": ORIGIN})
    assert r.status_code == 403
    r = env.client.post("/import", data={"path": "/x"}, headers={"Origin": ORIGIN, "X-CSRF-Token": "nope"})
    assert r.status_code == 403


def test_csrf_hidden_field_also_accepted(env):
    r = env.client.post("/import", data={"path": "/definitely/not/there", "csrf_token": env.csrf}, headers={"Origin": ORIGIN})
    assert r.status_code == 422


def test_non_form_content_type_is_refused(env):
    r = env.client.post("/import", content='{"path": "/x"}', headers={
        "Origin": ORIGIN, "X-CSRF-Token": env.csrf, "Content-Type": "application/json"})
    assert r.status_code == 415
    r = env.client.post("/import", content="path=/x", headers={
        "Origin": ORIGIN, "X-CSRF-Token": env.csrf, "Content-Type": "text/plain"})
    assert r.status_code == 415


def test_state_changing_routes_are_post_only(env):
    for path in ("/login/start", f"{env.base()}/scan/start", f"{env.base()}/review/start", f"{env.base()}/select/visible",
                 "/run/1/pause"):
        assert env.get(path).status_code in (404, 405)


def test_other_methods_are_refused(env):
    for method in ("PUT", "DELETE", "PATCH", "OPTIONS"):
        r = env.client.request(method, "/", headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST"})
        assert r.status_code == 405


# ---------------- headers ----------------


def test_security_headers_everywhere(env):
    pages = [env.get("/"), env.get("/help"), env.get("/static/htmx.min.js"), env.get("/static/style.css"),
             env.get("/nope"), env.client.get("/", headers={"Host": "evil.example"})]
    for r in pages:
        h = all_headers(r)
        csp = h["content-security-policy"]
        assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "script-src 'self'" in csp
        assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
        assert h["x-content-type-options"] == "nosniff"
        assert h["referrer-policy"] == "same-origin"
        assert not [k for k in h if k.startswith("access-control-")]
    assert all_headers(env.get("/"))["cache-control"] == "no-store"
    assert all_headers(env.get(f"{env.base()}/browse"))["cache-control"] == "no-store"


def test_no_cors_even_when_asked(env):
    r = env.client.get("/", headers={"Origin": "http://evil.example"})
    assert not [k for k in all_headers(r) if k.startswith("access-control-")]
    r = env.client.options("/", headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"})
    assert r.status_code == 405 and not [k for k in all_headers(r) if k.startswith("access-control-")]


def test_pages_have_no_inline_script_style_or_external_references(env):
    pages = ["/", "/help", "/login", "/import", env.base(), f"{env.base()}/verify", f"{env.base()}/browse"]
    env.post(f"{env.base()}/select/visible", {"type": "post"})
    pages.append(f"{env.base()}/review")
    for p in pages:
        html = env.get(p).text
        for tag in re.findall(r"<script\b[^>]*>", html):
            assert "src=" in tag, tag
        assert not re.search(r"<script[^>]*>\s*[^<\s]", html)
        assert not re.search(r"\son[a-z]+\s*=", html), "inline event handler"
        assert not re.search(r"\sstyle\s*=", html), "inline style attribute"
        assert "<style" not in html
        assert not re.search(r'(?:src|href)="https?://', html), "external reference"


# ---------------- static files ----------------


@pytest.mark.parametrize("path", ["/static/../state.py", "/static/%2e%2e/state.py", "/static/..%2fstate.py",
                                  "/static/%2e%2e%2fstate.py", "/static/security.py", "/static/", "/static/.",
                                  "/static//etc/passwd", "/static/..%5cstate.py", "/static/nope.js", "/static/htmx.min.js%00"])
def test_static_traversal_is_404(env, path):
    assert env.client.get(path).status_code == 404


def test_static_files_are_served(env):
    r = env.get("/static/htmx.min.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert env.get("/static/style.css").status_code == 200
    assert env.get("/static/htmx.LICENSE.txt").status_code == 200


# ---------------- nothing sensitive in logs or pages ----------------


def test_secrets_never_reach_logs(caplog):
    caplog.set_level(logging.DEBUG)
    e = Env(signed_in=False)
    e.client.get("/?launch=wrong-guess")
    e.sign_in()
    sid = e.client.cookies.get(COOKIE)
    e.get("/")
    e.get(f"{e.base()}/browse")
    e.post("/import", {"path": "/definitely/not/there"})
    e.client.get("/", headers={"Host": "evil.example"})
    # httpx logs the *test client's* own outgoing URLs (the harness, not the server): ignore that logger.
    text = "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith("httpx"))
    for secret in (SECRET, sid, e.csrf, "wrong-guess"):
        assert secret not in text


def test_session_and_launch_secret_not_in_pages(env):
    sid = env.client.cookies.get(COOKIE)
    for p in ("/", "/help", env.base(), f"{env.base()}/browse"):
        html = env.get(p).text
        assert SECRET not in html and sid not in html


def test_unknown_account_and_plan_are_404(env):
    assert env.get("/a/fixture/Zm9v").status_code == 404
    assert env.get("/a/nosuchplatform/Zm9v").status_code == 404
    assert env.get("/a/fixture/%%%").status_code == 404
    assert env.get("/results/999").status_code == 404
    assert env.get("/plan/999/export").status_code == 404
    assert env.get("/plan/999/report/text").status_code == 404
