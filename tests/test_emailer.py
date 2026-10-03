import pytest

from atlas.emailer import EmailError, ResendEmailer


class _Resp:
    def __init__(self, status: int, text: str = ""):
        self.status_code = status
        self.text = text


def test_send_posts_resend_payload():
    captured: dict = {}

    def transport(method, url, *, headers, json):
        captured.update(method=method, url=url, headers=headers, json=json)
        return _Resp(200)

    emailer = ResendEmailer("key", "from@x.com", "to@y.com", transport=transport)
    assert emailer.send("Subj", "<b>hi</b>", "hi") is True
    assert captured["method"] == "POST"
    assert captured["url"].startswith("https://api.resend.com")
    assert captured["json"]["to"] == ["to@y.com"]
    assert captured["headers"]["Authorization"] == "Bearer key"


def test_send_requires_configuration():
    emailer = ResendEmailer("", "", "", transport=lambda *a, **k: _Resp(200))
    with pytest.raises(EmailError):
        emailer.send("s", "h")


def test_send_raises_on_error_status():
    emailer = ResendEmailer("k", "f", "t", transport=lambda *a, **k: _Resp(422, "bad"))
    with pytest.raises(EmailError):
        emailer.send("s", "h")
