"""Email digest over SMTP. Settings come from the environment (or a local .env file):

    SMTP_HOST      default smtp.gmail.com
    SMTP_PORT      default 465 (implicit TLS); 587 uses STARTTLS
    SMTP_USER      login, usually your address
    SMTP_PASSWORD  for Gmail, an app password (Google Account > Security > App passwords)
    MAIL_TO        comma-separated recipients, default SMTP_USER
    MAIL_FROM      default SMTP_USER
"""
from __future__ import annotations

import html
import os
import smtplib
import ssl
from collections import defaultdict
from email.message import EmailMessage

from .ats import Job


def load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def email_configured() -> bool:
    return bool(os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASSWORD"))


def subject(new: list[Job]) -> str:
    companies = list(dict.fromkeys(j.company for j in new))
    names = ", ".join(companies[:3]) + (f" +{len(companies) - 3} more" if len(companies) > 3 else "")
    return f"{len(new)} new job{'s' * (len(new) != 1)}: {names}"


def _by_company(jobs: list[Job]) -> dict[str, list[Job]]:
    groups = defaultdict(list)
    for j in sorted(jobs, key=lambda j: (j.company.lower(), j.title.lower())):
        groups[j.company].append(j)
    return groups


def render_text(new: list[Job], problems: list[str]) -> str:
    lines = []
    for company, jobs in _by_company(new).items():
        lines.append(f"{company} ({len(jobs)})")
        for j in jobs:
            meta = " · ".join(x for x in (j.location, "remote" if j.remote else "", j.posted and f"posted {j.posted}") if x)
            lines += [f"  {j.title}", *([f"    {meta}"] if meta else []), f"    {j.url}"]
        lines.append("")
    if problems:
        lines += ["Boards needing attention:", *[f"  - {p}" for p in problems]]
    return "\n".join(lines).strip() + "\n"


def render_html(new: list[Job], problems: list[str]) -> str:
    e = html.escape
    parts = ['<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;font-size:14px;color:#1a1a1a;max-width:680px">']
    for company, jobs in _by_company(new).items():
        parts.append(f'<h3 style="margin:18px 0 6px;font-size:15px">{e(company)} <span style="color:#777;font-weight:normal">({len(jobs)})</span></h3>')
        for j in jobs:
            meta = " · ".join(e(x) for x in (j.location, "remote" if j.remote else "", j.posted and f"posted {j.posted}") if x)
            parts.append(f'<div style="margin:0 0 8px"><a href="{e(j.url, quote=True)}" style="color:#0b57d0;text-decoration:none;font-weight:600">{e(j.title)}</a>'
                         f'<div style="color:#666;font-size:12px">{meta}</div></div>')
    if problems:
        parts.append('<h4 style="margin:22px 0 6px;color:#a4262c">Boards needing attention</h4><ul style="color:#555;font-size:12px;padding-left:18px">')
        parts += [f"<li>{e(p)}</li>" for p in problems]
        parts.append("</ul>")
    parts.append("</div>")
    return "".join(parts)


def send(new: list[Job], problems: list[str], subject_line: str | None = None) -> None:
    env = lambda k, default=None: os.environ.get(k) or default  # CI passes unset secrets as ""
    user, password = env("SMTP_USER"), env("SMTP_PASSWORD")
    host = env("SMTP_HOST", "smtp.gmail.com")
    port = int(env("SMTP_PORT", "465"))
    to = [a.strip() for a in env("MAIL_TO", user).split(",") if a.strip()]
    if host == "smtp.gmail.com":
        password = password.replace(" ", "")  # Google shows app passwords as "abcd efgh ijkl mnop"

    msg = EmailMessage()
    msg["Subject"] = subject_line or (subject(new) if new else f"Job poller: {len(problems)} board(s) need attention")
    msg["From"] = env("MAIL_FROM", user)
    msg["To"] = ", ".join(to)
    msg.set_content(render_text(new, problems))
    msg.add_alternative(render_html(new, problems), subtype="html")

    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(user, password)
            s.send_message(msg)
