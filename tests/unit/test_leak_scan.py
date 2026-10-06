"""The leak scanner itself (M1.23; AC-M1.4): it finds a planted secret on every surface kind.

Every value is fictional and built at run time (``tests/leak_scan.py``).
"""

from httpx import Request, Response

from tests.leak_scan import TOKEN_PREFIX, WINDOW, Evidence, fictional_secret, find_leaks


def test_leak_scan_finds_a_planted_secret() -> None:
    """The scanner itself: a whole value in a response header, a whole value and a window
    reaching into the body in texts, and a UTF-16 copy in a file are all found; a window wholly
    inside the public prefix is not."""
    secret = fictional_secret(TOKEN_PREFIX, "planted")
    body_window = secret.value[len(TOKEN_PREFIX) + 5 : len(TOKEN_PREFIX) + 5 + WINDOW]
    evidence = Evidence()
    request = Request("GET", "http://testserver/planted")
    evidence.add_response(
        "header", Response(200, headers={"X-Planted": secret.value}, request=request)
    )
    evidence.add_response("clean response", Response(200, text="nothing here", request=request))
    evidence.texts.append(("whole", f"x {secret.value} y"))
    evidence.texts.append(("window", f"...{body_window}..."))
    evidence.texts.append(("prefix only", TOKEN_PREFIX + " is the token format"))
    evidence.blobs.append(("utf16", ("z" + secret.value[20:30]).encode("utf-16-le")))
    leaks = find_leaks(secret, evidence)
    assert [leak.split(":")[0] for leak in leaks] == [
        "planted in header",
        "planted in whole",
        "planted in window",
        "planted in utf16",
    ]
    assert "header x-planted" in leaks[0], leaks[0]
    assert all(secret.value not in leak for leak in leaks), "reports never repeat the value"
