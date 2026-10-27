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


def _report(new, problems, alert):
    from types import SimpleNamespace
    return SimpleNamespace(new=new, problems=problems, alert_problems=alert)


def test_one_recipient_list_gets_one_combined_email(monkeypatch):
    from jobpoller.__main__ import mails
    monkeypatch.setenv("SMTP_USER", "me@x.com")
    monkeypatch.delenv("MAIL_TO", raising=False)
    monkeypatch.delenv("ALERTS_TO", raising=False)
    assert mails(_report(["job"], ["Acme: failing"], False)) == [(["me@x.com"], ["job"], ["Acme: failing"])]


def test_friends_on_mail_to_get_postings_but_not_board_alerts(monkeypatch):
    from jobpoller.__main__ import mails
    monkeypatch.setenv("SMTP_USER", "me@x.com")
    monkeypatch.setenv("MAIL_TO", "me@x.com, friend@y.com")
    monkeypatch.delenv("ALERTS_TO", raising=False)  # defaults to SMTP_USER
    assert mails(_report(["job"], ["Acme: failing"], True)) == [
        (["me@x.com", "friend@y.com"], ["job"], []),
        (["me@x.com"], [], ["Acme: failing"]),
    ]
    # a board still failing, but no threshold just crossed: postings only, nothing to me separately
    assert mails(_report(["job"], ["Acme: failing"], False)) == [(["me@x.com", "friend@y.com"], ["job"], [])]
    # no new postings: only the alert, only to me
    assert mails(_report([], ["Acme: failing"], True)) == [(["me@x.com"], [], ["Acme: failing"])]


def test_a_profile_gets_its_own_email_only_when_its_secret_is_set(monkeypatch):
    from types import SimpleNamespace
    from jobpoller.__main__ import mails
    monkeypatch.setenv("SMTP_USER", "me@x.com")
    monkeypatch.delenv("MAIL_TO", raising=False)
    monkeypatch.delenv("ALERTS_TO", raising=False)
    prof = SimpleNamespace(name="robotics", to_env="MAIL_TO_ROBOTICS")
    report = SimpleNamespace(new=["job"], problems=[], alert_problems=False, profile_new={"robotics": ["job", "robot job"]})
    monkeypatch.delenv("MAIL_TO_ROBOTICS", raising=False)
    assert mails(report, [prof]) == [(["me@x.com"], ["job"], [])]  # not set: never falls back to me
    monkeypatch.setenv("MAIL_TO_ROBOTICS", "her@z.com")
    assert mails(report, [prof]) == [(["me@x.com"], ["job"], []), (["her@z.com"], ["job", "robot job"], [])]


def test_a_run_with_only_profile_postings_still_emails_the_profile(monkeypatch):
    from types import SimpleNamespace
    from jobpoller.__main__ import mails
    monkeypatch.setenv("SMTP_USER", "me@x.com")
    monkeypatch.delenv("MAIL_TO", raising=False)
    monkeypatch.delenv("ALERTS_TO", raising=False)
    monkeypatch.setenv("MAIL_TO_ROBOTICS", "her@z.com")
    prof = SimpleNamespace(name="robotics", to_env="MAIL_TO_ROBOTICS")
    report = SimpleNamespace(new=[], problems=["Acme: still failing"], alert_problems=False, profile_new={"robotics": ["robot job"]})
    assert mails(report, [prof]) == [(["her@z.com"], ["robot job"], [])]  # nothing for me: no new postings, no new alert
