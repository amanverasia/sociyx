from unpost.redact import scrub


def test_bearer_and_dpop_tokens_removed():
    out = scrub("Authorization: Bearer abc123SECRET.tok")
    assert "abc123SECRET" not in out
    out = scrub("Authorization: DPoP s3cr3ttokenvalue")
    assert "s3cr3t" not in out


def test_jwt_removed():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJkaWQ6cGxjOngifQ.c2lnbmF0dXJl"
    assert jwt not in scrub(f"token was {jwt} ok")


def test_app_password_removed():
    assert "abcd-efgh-ijkl-mnop" not in scrub("using abcd-efgh-ijkl-mnop to log in")


def test_json_password_and_refresh_token():
    out = scrub('{"password": "hunter2hunter2", "user": "bob"}')
    assert "hunter2" not in out and "bob" in out
    assert "r3fr3sh" not in scrub("refresh_token=r3fr3sh&x=1")
    assert "r3fr3sh" not in scrub("'refresh_token': 'r3fr3sh'")


def test_normal_text_untouched():
    s = "Deleted 12 posts from alice.fixture in 3.5s (page 4/9) https://example.com/a/b"
    assert scrub(s) == s
