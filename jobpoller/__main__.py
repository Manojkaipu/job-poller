"""python -m jobpoller {run,list,check,discover}"""
from __future__ import annotations

import argparse
import datetime as dt
import sys

from . import notify
from .discover import discover
from .poller import fetch_all, load_config, print_jobs, run, summarize
from .state import State


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    state = State(cfg.state_file)
    report = run(cfg, state)
    summarize(report)
    if report.new:
        print()
        print_jobs(report.new)

    if args.dry_run:
        print("\n(dry run: state not saved, no email)")
        return 0

    outgoing = mails(report, cfg.profiles)
    if outgoing and not args.no_email:
        if notify.email_configured():
            for to, new, problems in outgoing:
                notify.send(new, problems, to=to)
                print(f"emailed {', '.join(to)}")
        else:
            print("email not configured (set SMTP_USER and SMTP_PASSWORD); printed only")
    # Saved only after the email went out, so a failed send is retried next run.
    state.save()
    return 0


def mails(report, profiles=()) -> list[tuple[list[str], list, list[str]]]:
    """(recipients, new postings, board problems) for each email this run sends.

    Postings go to MAIL_TO and board problems to ALERTS_TO. With one list for both (just me),
    it's one email as before: postings with any failing boards listed underneath. With a friend
    on MAIL_TO, postings go to everyone, and the problems go to ALERTS_TO in their own email
    only when a board has just crossed its alert threshold."""
    jobs_to, alerts_to = notify.recipients()
    if set(jobs_to) == set(alerts_to):
        combined = [(jobs_to, report.new, report.problems)] if report.new or report.alert_problems else []
        return combined + _profile_mails(report, profiles)
    out = [(jobs_to, report.new, [])] if report.new else []
    if report.alert_problems:
        out.append((alerts_to, [], report.problems))
    return out + _profile_mails(report, profiles)


def _profile_mails(report, profiles) -> list[tuple[list[str], list, list[str]]]:
    """Each profile (say, someone who also wants robotics roles) gets its own email with every
    posting its filters match: the base ones and its extras. Keep its recipients out of MAIL_TO,
    or they get both emails."""
    out = []
    for p in profiles:
        to, jobs = notify.profile_recipients(p.to_env), report.profile_new.get(p.name, [])
        if to and jobs:
            out.append((to, jobs, []))
    return out


def cmd_list(args) -> int:
    cfg = load_config(args.config)
    companies = [c for c in cfg.companies if not args.company or args.company.lower() in c.name.lower()]
    jobs, started = [], dt.datetime.now(dt.timezone.utc)
    for f in fetch_all(companies, cfg.workers):
        if f.error:
            print(f"! {f.company.name}: {f.error}", file=sys.stderr)
        jobs += [j for j in f.jobs if (args.all or cfg.filters.match(j))
                 and (not args.fresh or (j.posted and cfg.filters.fresh(j, started)))]
    print_jobs(jobs)
    print(f"\n{len(jobs)} posting(s)" + ("" if args.all else " matching the filters")
          + (f", posted in the last {cfg.filters.max_age_hours:g}h" if args.fresh else ""))
    return 0


def cmd_check(args) -> int:
    cfg = load_config(args.config)
    bad = 0
    for f in sorted(fetch_all(cfg.companies, cfg.workers), key=lambda f: f.company.name.lower()):
        if f.error:
            bad += 1
            print(f"FAIL {f.company.name:28} {f.company.key:45} {f.error}")
        else:
            m = sum(cfg.filters.match(j) for j in f.jobs)
            flag = "EMPTY" if not f.jobs else "ok"
            print(f"{flag:5}{f.company.name:28} {f.company.key:45} {len(f.jobs):5} postings, {m:3} match  {f.seconds:4.1f}s")
    print(f"\n{len(cfg.companies) - bad}/{len(cfg.companies)} boards reachable")
    return 1 if bad else 0


def cmd_test_email(args) -> int:
    if not notify.email_configured():
        print("email not configured: set SMTP_USER and SMTP_PASSWORD in .env")
        return 1
    cfg = load_config(args.config)
    filters, to = cfg.filters, notify.recipients()[0]
    if args.profile:
        p = next((p for p in cfg.profiles if p.name == args.profile), None)
        if not p:
            print(f"no profile {args.profile!r}; config.toml has: {', '.join(x.name for x in cfg.profiles) or 'none'}")
            return 1
        filters, to = p, notify.profile_recipients(p.to_env)
        if not to:
            print(f"{p.to_env} isn't set, so profile {p.name!r} has no one to send to")
            return 1
    # A sample of what a real alert looks like: matching postings from the last max_age_hours,
    # taken from fast boards. (Not ones the poller hasn't seen; that's what `run` sends.)
    sample = [c for c in cfg.companies if c.ats in ("greenhouse", "ashby")][:80]
    now = dt.datetime.now(dt.timezone.utc)
    jobs = [j for f in fetch_all(sample, cfg.workers) for j in f.jobs
            if filters.match(j) and j.posted and filters.fresh(j, now)][:5]
    notify.send(jobs, [], to=to, subject_line=f"Job poller test: {len(jobs)} matching postings from the last "
                                              f"{filters.max_age_hours:g}h (a sample, not new alerts)")
    print(f"sent a test email with {len(jobs)} postings to {', '.join(to)}")
    return 0


def cmd_discover(args) -> int:
    for name in args.names:
        hits = discover(name, args.slug or [])
        if not hits:
            print(f"{name}: not found on any supported board (try --slug, or check their careers page)")
        for h in hits:
            print(f'{name}: {h["ats"]} slug="{h["slug"]}"  {h["count"]} postings  {h["url"]}')
            for s in h["sample"]:
                print(f"    {s}")
    return 0


def main(argv=None) -> int:
    notify.load_dotenv()
    p = argparse.ArgumentParser(prog="jobpoller", description=__doc__)
    p.add_argument("--config", default="config.toml")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="poll every board, report and email new matching postings")
    r.add_argument("--dry-run", action="store_true", help="don't save state or send email")
    r.add_argument("--no-email", action="store_true", help="save state but only print")
    r.set_defaults(fn=cmd_run)

    ls = sub.add_parser("list", help="show postings that match the filters right now (ignores state)")
    ls.add_argument("--company", help="substring of a company name")
    ls.add_argument("--all", action="store_true", help="ignore the filters")
    ls.add_argument("--fresh", action="store_true", help="only postings dated within max_age_hours")
    ls.set_defaults(fn=cmd_list)

    sub.add_parser("check", help="fetch every board and report counts and errors").set_defaults(fn=cmd_check)
    te = sub.add_parser("test-email", help="send a sample email to check the SMTP settings")
    te.add_argument("--profile", help="send the sample a profile's recipients would get, to them")
    te.set_defaults(fn=cmd_test_email)

    d = sub.add_parser("discover", help="find which ATS a company uses")
    d.add_argument("names", nargs="+")
    d.add_argument("--slug", action="append", help="extra slug to try (repeatable)")
    d.set_defaults(fn=cmd_discover)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
