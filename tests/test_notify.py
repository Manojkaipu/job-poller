from jobpoller import notify
from jobpoller.ats import Job


def jobs():
    return [Job("Beta", "1", "ML Engineer <Search>", "https://b/1", location="Austin, TX", remote=True, posted="2026-09-27"),
            Job("Alpha", "2", "Data Scientist", "https://a/2?x=1&y=2"),
            Job("Gamma", "3", "SWE", "https://g/3"), Job("Delta", "4", "SWE", "https://d/4")]


def test_subject():
    assert notify.subject(jobs()) == "4 new jobs: Beta, Alpha, Gamma +1 more"
    assert notify.subject(jobs()[:1]) == "1 new job: Beta"


def test_html_is_escaped_and_grouped():
    h = notify.render_html(jobs(), ["Acme (lever:acme): failing"])
    assert "ML Engineer &lt;Search&gt;" in h and "<Search>" not in h
    assert 'href="https://a/2?x=1&amp;y=2"' in h
    assert h.index("Alpha") < h.index("Beta") < h.index("Delta")
    assert "Boards needing attention" in h


def test_text_version():
    t = notify.render_text(jobs()[:2], [])
    assert t.splitlines()[:4] == ["Alpha (1)", "  Data Scientist", "    https://a/2?x=1&y=2", ""]
    assert "Austin, TX · remote · posted 2026-09-27" in t


def test_posted_label():
    import datetime as dt
    now = dt.datetime(2026, 9, 28, 18, 0, tzinfo=dt.timezone.utc)
    job = lambda p: Job("A", "1", "x", "https://x", posted=p)
    assert notify.posted_label(job("2026-09-28T17:35:00Z"), now) == "posted 25m ago"
    assert notify.posted_label(job("2026-09-28T09:00:00Z"), now) == "posted 9h ago"
    assert notify.posted_label(job("2026-09-20T09:00:00Z"), now) == "posted 2026-09-20"
    assert notify.posted_label(job("2026-09-27"), now) == "posted 2026-09-27"
    assert notify.posted_label(job(None), now) == ""


def test_dotenv_does_not_override_real_env(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("SMTP_USER=file@example.com\n# comment\nMAIL_TO='a@x.com, b@x.com'\n")
    monkeypatch.setenv("SMTP_USER", "env@example.com")
    monkeypatch.delenv("MAIL_TO", raising=False)
    notify.load_dotenv(str(p))
    import os
    assert os.environ["SMTP_USER"] == "env@example.com"
    assert os.environ["MAIL_TO"] == "a@x.com, b@x.com"
