# job-poller

Checks the public job boards of 147 companies every 4 hours, keeps the postings whose title and location match what I'm looking for, and emails me the ones that weren't there on the last run.

Job sites and LinkedIn alerts often lag the company's own board, and they cover smaller companies unevenly. Most companies post through an applicant tracking system (ATS) that serves its job board from a public JSON endpoint, so polling those directly gets a posting within hours of it going up.

Pure Python standard library, no dependencies.

## Supported boards

I went through the ~90 tools listed on [everyats.com](https://everyats.com/) and kept the ones that (a) tech startups and mid-size companies actually use and (b) serve their job board from a public endpoint that needs no key:

| ATS | endpoint | `slug` is | e.g. |
|---|---|---|---|
| Greenhouse | `boards-api.greenhouse.io/v1/boards/{slug}/jobs` | board token | Anthropic, Databricks, Waymo |
| Ashby | `api.ashbyhq.com/posting-api/job-board/{slug}` | board name | OpenAI, Notion, Cursor |
| Lever | `api.lever.co/v0/postings/{slug}` (`eu:` prefix for the EU instance) | company | Palantir, Zoox |
| Workable | `apply.workable.com/api/v1/widget/accounts/{slug}` | account | Hugging Face |
| SmartRecruiters | `api.smartrecruiters.com/v1/companies/{slug}/postings` | company id | ServiceNow, Canva |
| Recruitee | `{slug}.recruitee.com/api/offers/` | subdomain | |
| BambooHR | `{slug}.bamboohr.com/careers/list` | subdomain | GitKraken |
| Breezy HR | `{slug}.breezy.hr/json` | subdomain | |
| Pinpoint | `{slug}.pinpointhq.com/postings.json` | subdomain | |
| Rippling | `api.rippling.com/platform/api/ats/v1/board/{slug}/jobs` | board | Rippling |
| Gem | `api.gem.com/job_board/v0/{slug}/job_posts/` | board | Retool |
| Dover | `app.dover.com/api/v1/careers-page/{id}/jobs` | careers-page slug | |
| Manatal | `api.manatal.com/open/v3/career-page/{slug}/jobs/` | career page | Qdrant |
| Teamtailor | `{slug}.teamtailor.com/jobs.rss` | subdomain or full host | |
| Workday | `{host}/wday/cxs/{tenant}/{site}/jobs` | careers-site URL, e.g. `nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite` | NVIDIA, Intel, Adobe |

Every one of these was checked against a live board before the fetcher was written, and the tests run each fetcher against a recorded response.

`companies.toml` has 147 companies: AI and ML startups (49), autonomy and defense (8), data, infrastructure and developer tools (48), fintech (9), consumer tech (18), Phoenix-area employers (6) and a few large companies on Workday and SmartRecruiters (9). On 2026-09-28 they had 31,535 open postings between them; a full run takes about 2 minutes, most of it NVIDIA's Workday board.

What I left out from that list, and why:
* **No public feed:** JazzHR (its export feed now returns 410 Gone), Getro (the API needs the network owner's key), iCIMS, Taleo / Oracle, SAP SuccessFactors, Avature, Phenom, Eightfold, Cornerstone, Brassring, UKG, ADP, Paycor. Their boards are server-rendered pages or need per-tenant scraping.
* **Not job boards:** staffing-agency CRMs (Bullhorn, JobDiva, Loxo, Vincere, Recruit CRM, Recruiterflow, TrackerRMS, Mercury, Ezekia, Clockwork), sourcing tools (Juicebox, ATZ CRM), screening add-ons (ApplicantAI, TalentClerk, Spark Hire, Typeform) and HR/payroll suites (Deel, Gusto, TriNet, Zoho, iSolved, Sage).
* **Wrong market:** schools (Frontline, Every by Iris), healthcare and hourly hiring (Apploi, Fountain, TalentReef, Paradox), public sector (JobAps), and small regional tools (Glorri, Folks, Hirefly, Talexio).

## Setup

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"   # Python 3.11+
python -m pytest -q
```

Email goes out over SMTP. Put the settings in a `.env` file (gitignored) or the environment:

```
SMTP_USER=you@gmail.com
SMTP_PASSWORD=your-app-password     # Gmail: Google Account > Security > 2-Step Verification > App passwords
MAIL_TO=you@gmail.com               # optional, comma-separated
# SMTP_HOST=smtp.gmail.com  SMTP_PORT=465  (defaults; port 587 uses STARTTLS)
```

Without SMTP settings, new postings are just printed.

## Use

```bash
python -m jobpoller check                    # fetch every board: posting counts, matches, errors
python -m jobpoller list                     # what matches the filters right now
python -m jobpoller list --company openai --all
python -m jobpoller run --dry-run            # a real run without saving state or emailing
python -m jobpoller run                      # the scheduled command
python -m jobpoller discover "Company Name"  # which ATS does a company use?
```

**Adding a company.** `discover` tries likely slugs on every supported board and prints the hits with sample titles, so you can tell the right board from a namesake:

```
$ python -m jobpoller discover Retool
Retool: gem slug="retool"  24 postings  https://jobs.gem.com/retool
```

Then add it to `companies.toml`:

```toml
[[company]]
name = "Retool"
ats = "gem"
slug = "retool"
```

For Workday or a SmartRecruiters company, open their careers page and copy the host/site or company id from the URL.

**Filters** are in `config.toml`. A title has to contain one of the `include` terms and none of the `exclude` terms, as whole words: `ml` matches "ML Engineer" but not "HTML". Location is checked against US states, state codes, major cities and country names, so "US, CA, Santa Clara", "New York City, NY" and "Remote - US" pass while "London", "Toronto, ON" and "Remote - EMEA" don't. A posting with no location, or plain "Remote", is kept.

## Scheduling

**GitHub Actions** (recommended, since it runs when the laptop is asleep): `.github/workflows/poll.yml` runs every 4 hours. Add `SMTP_USER`, `SMTP_PASSWORD` and optionally `MAIL_TO` as repository secrets. The state file is kept in the Actions cache, not committed, so the repo doesn't grow a commit per run. GitHub pauses scheduled workflows in a repo with no activity for 60 days; re-enable it from the Actions tab if that happens.

**Windows Task Scheduler**, if you'd rather run it locally:

```powershell
schtasks /Create /TN JobPoller /SC HOURLY /MO 4 /TR "\"C:\path\to\Job_Poller\scripts\poll.cmd\""
```

`scripts/poll.cmd` runs the poller from the repo's `.venv` and appends to `state\poll.log`.

Use one or the other: each keeps its own state.

## How it works

* **Fetch.** Every board is fetched in parallel (8 at a time), with retries and `Retry-After` handling for rate limits. A board that errors, or changes its format so parsing fails, is skipped for that run without affecting the others.
* **Diff.** `state/seen.json` remembers every posting id per board with when it was first and last seen, including postings the filters rejected. So widening the filters later doesn't report old postings as new.
* **First run is silent.** The first time a board is fetched, its current postings are recorded without alerting, so adding a company with 800 open roles doesn't send 800 alerts. Only postings that appear after that are reported.
* **Outages aren't removals.** A failed fetch leaves the board's known postings untouched, so a 503 followed by a recovery doesn't re-announce everything. After 3 failed runs in a row the board is flagged in the email, as is a board that suddenly returns 0 postings (usually a moved or renamed board).
* **Forgetting.** A posting that has been gone for 45 days is dropped from the state; if it's re-posted after that it's reported again. Removing a company from the config drops its state.
* **Email last, save after.** The state is saved only after the email is sent, so a failed send is retried on the next run instead of being lost.
* **Size limit.** A board with more than 5,000 postings is refused as probably not a company (Mercor's contractor marketplace on Manatal has 16,000) rather than paged through for minutes.

## Limitations

* Filtering is on title and location only. Descriptions aren't fetched for every board, so "no visa sponsorship" or years-of-experience requirements aren't checked.
* Workday's list gives no posting dates, and large Workday boards take ~100 requests per run (20 postings per page). NVIDIA reports exactly 2,000 postings, which looks like a cap on Workday's side, so the tail of its board may be missed.
* Companies that host their own careers site without one of these ATSs (Google, Meta, Apple, Microsoft, Amazon) aren't covered.
* Location parsing is heuristic. "Remote" with no country counts as US; an unusual format may be misread. `extra_locations` and `us_only = false` are the escape hatches.

## How I used AI tools

I used Claude (via Claude Code) to write the fetchers, filters, tests and this README, and to find which board each company uses. The idea, the choice of ATSs and companies, and the filters are mine to tune.

Things that went wrong while building it, and the check that caught each:
* **A board that never ended.** Mercor's Manatal board is its contractor marketplace, with ~16,600 postings served 10 at a time, and the first discovery pass hung on it. Timing each attempt found it; boards over 5,000 postings are now refused, and Manatal is fetched 100 per page.
* **Namesakes.** Name-based discovery found Figure Lending for Figure AI, a UK telesales firm for Together AI, a Dutch hotel group for Clay and a Brazilian bank for Neon. Every hit was checked against its sample titles before going into `companies.toml`.
* **"Hybrid" isn't a place.** Cloudflare's board puts the work arrangement in the location field, so its postings were all rejected as non-US. Listing the engineering postings the filters rejected on boards with suspiciously few matches caught it.
* **Seven wrong Workday URLs.** Guessed site names for Dell, Qualcomm, Netflix and others return HTTP 422; only the eight that answered went in.
