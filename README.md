# Letter to the Future — Backend ✉️

FastAPI service behind [Letter to the Future](https://github.com/peterkahumu/letter-to-the-future-frontend).
It accepts letters, emails the recipient for confirmation, and delivers the
letter on the chosen date.

**Frontend repository:** [letter-to-the-future-frontend](https://github.com/peterkahumu/letter-to-the-future-frontend)

## Stack

| Piece | Choice |
|---|---|
| API | FastAPI |
| ORM | SQLAlchemy 2.x (async) + asyncpg |
| Database | PostgreSQL (Neon in production) |
| Migrations | Alembic |
| Mail | `smtplib` + Gmail today; Resend and a console transport also wired |
| Media | Cloudinary |
| Scheduler | `fastapi_utilities.repeat_at`, hourly, in-process |
| Rate limiting | slowapi, per-process |

## How a letter works

```
written → pending → (recipient confirms) → scheduled → sent
                                                    ↘ failed     (gave up after MAX_DELIVERY_ATTEMPTS)
                                                    ↘ cancelled  (withdrawn by the writer)
```

Nothing is queued until the recipient clicks the confirmation link. That
double opt-in is what stops the service being used to mail strangers.

Links are the credential — there are no user accounts. Confirmation and
manage tokens are random, emailed once, and stored only as SHA-256 hashes,
so a database leak yields hashes rather than working links. Unsubscribe
links are instead an HMAC of the address under `SECRET_KEY`, which is why
rotating that key invalidates unsubscribe links already in the wild
(confirmation and cancel links are unaffected).

## Local development

The quickest loop needs no real database and no real SMTP.

```bash
# 1. Dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Local Postgres on port 5433, plus an optional mail catcher
docker compose -f ../docker-compose.dev.yml up -d

# 3. Configuration
cp .env.example .env      # fill in the blanks

# 4. Schema
alembic upgrade head

# 5. Run
uvicorn app.main:app --reload --port 8000
```

Interactive API docs: <http://localhost:8000/docs>

### Reading emails locally

Set `EMAIL_PROVIDER=console` and emails are printed to your terminal
instead of sent, with any links listed separately so you can click the
confirmation link without configuring SMTP.

To read the real HTML instead, the dev compose file includes
[Mailpit](https://mailpit.axllent.org/): set `EMAIL_PROVIDER=smtp`,
`SMTP_HOST=localhost`, `SMTP_PORT=1025`, `SMTP_INSECURE=true`, then open
<http://localhost:8025>.

### Triggering delivery without waiting an hour

```bash
python scripts/run_scheduler.py
```

Runs a single scheduler pass and exits.

## Configuration

Every setting is an environment variable. `.env.example` is the reference —
it lists what's required in production, what's required locally, and what's
optional, with the defaults.

Values are resolved in this order, first match winning:

1. Real environment variables (what the deployed platform sets)
2. `.env.local`
3. `.env`

`.env.local` layers on top of `.env` (`config.py`), so a local run can point
at a local database and the console mail transport without editing values
you also deploy. Both files are gitignored.

Three that cause confusing behaviour if they're wrong:

- **`FRONTEND_URL`** — the first comma-separated entry builds every
  confirmation, cancel, and unsubscribe link *inside emails*. Wrong value
  means recipients get links to somewhere that isn't your site, and there is
  no error to notice: an empty value silently falls back to localhost.
- **`SMTP_PORT`** — must be `465`. The SMTP path uses implicit SSL, so 587
  hangs rather than failing.
- **`SECRET_KEY`** — leave it unset and it defaults to a placeholder that is
  public in this repo, letting anyone forge unsubscribe links for any
  address. **It now also signs every account access token**, so a placeholder
  value means anyone can mint a token for any account, including an admin
  one. Always set it in production. Changing it invalidates existing
  unsubscribe links and signs everyone out — which is also the emergency
  lever if you think a token has leaked.

Two more that matter once accounts exist:

- **`BOOTSTRAP_ADMIN_EMAIL`** — whoever registers with this address is
  created as an admin. That is how the first admin exists at all, since only
  an admin can promote anyone. Set it, register, then blank it: leaving it
  set means anyone who can read your environment knows which address to try
  to take over.
- **`REQUIRE_EMAIL_VERIFICATION`** — on by default, and should stay on
  anywhere real. Turn it off locally, where mail goes to the console and
  confirming is an extra hop for nothing.

## API

Letter routes are under `/api/letters`; accounts, journals, and the admin
surface follow below.

### Letters

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/letters` | Write a letter. Emails the recipient for confirmation; schedules nothing yet. |
| `POST` | `/api/letters/{id}/confirm?token=` | Recipient confirms. Moves `pending → scheduled`. One-time use. |
| `GET` | `/api/letters/{id}?token=` | Letter status. Address always masked; the token proves you may cancel. |
| `DELETE` | `/api/letters/{id}?token=` | Cancel before delivery. |
| `GET` | `/api/letters/feed/public?limit=&cursor=` | Delivered letters whose writers chose to share them. Cursor paginated. |
| `POST` | `/api/letters/{id}/report` | Report a public letter. |
| `POST` | `/api/letters/unsubscribe?email=&t=` | Suppress an address and cancel everything queued for it. |
| `POST` | `/api/letters/upload` | Upload media to Cloudinary. Type and size capped. |

### Accounts — `/api/auth`

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/auth/register` | Create an account and email a verification link. Answers identically whether or not the address is taken. |
| `POST` | `/api/auth/verify-email` | Spend a verification token; returns a signed-in session. One-time use. |
| `POST` | `/api/auth/resend-verification` | Fresh verification link for the signed-in account. |
| `POST` | `/api/auth/login` | Email + password → access and refresh tokens. |
| `POST` | `/api/auth/refresh` | Rotate the pair. Reusing a rotated token revokes every session. |
| `POST` | `/api/auth/logout` | Revoke one refresh token. Unauthenticated — the token is the credential. |
| `POST` | `/api/auth/logout-all` | End every session for the account. |
| `GET` | `/api/auth/sessions` | Where this account is signed in. |
| `POST` | `/api/auth/forgot-password` | Email a one-time reset link. Always answers the same, found or not. |
| `POST` | `/api/auth/reset-password` | Spend a reset token; signs every device out. |
| `POST` | `/api/auth/change-password` | Change it from inside the account; signs every device out. |
| `GET` `PATCH` `DELETE` | `/api/auth/me` | Read, rename, or delete the account. |

Two credentials, deliberately different in kind. The **access token** is a
short-lived signed JWT the server never stores — fast to check, and for the
same reason impossible to recall before it expires. The **refresh token** is
an opaque string whose hash is a row in `refresh_tokens`, which is what makes
logout and suspension actually end a session. Access tokens are therefore
kept short, and the user row is re-read on every request so a deletion or
suspension bites immediately.

Rotation reuse is treated as theft and revokes everything. An explicit
logout is not — both leave `revoked_at` set, so `revoked_reason` is what
separates a thief replaying a token from a stale client retrying one.
Without that distinction, signing out on a laptop signs out the phone.

### Dashboard — `/api/dashboard`

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/dashboard` | Counts, next delivery, streak, and recent letters and entries in one request. |
| `GET` | `/api/dashboard/letters?status=` | Letters this account wrote. Read-only. |

### Journals — `/api/journals`

| Method | Path | Purpose |
|---|---|---|
| `GET` `POST` | `/api/journals` | List or create notebooks. |
| `GET` `PATCH` `DELETE` | `/api/journals/{id}` | Read, edit, or delete one, with its entries. |
| `GET` `POST` | `/api/journals/{id}/entries` | List (excerpts, searchable) or write entries. |
| `GET` `PATCH` `DELETE` | `/api/journals/entries/{id}` | Read, **edit**, or delete one entry. |
| `GET` | `/api/journals/entries/all/recent` | One timeline across every notebook — the diary view. |

Everything here is private to its author, and ownership checks answer `404`
rather than `403` so an id is never confirmed to a stranger. Entry bodies go
through the same sanitizer as letter bodies.

Note the asymmetry with letters: entries have a `PATCH`, letters have none
anywhere in this API. That is the feature, not an omission — `tests/
test_journals.py` asserts on the route table so a later `PUT` on a letter
fails the suite.

### Admin — `/api/admin`

Every route requires the admin role, re-read from the database per request,
and answers `404` to everyone else so the surface doesn't announce itself.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/admin/stats` | Site-wide counts. |
| `GET` | `/api/admin/users` | Users with counts of what they've written. Searchable, paginated. |
| `GET` `PATCH` `DELETE` | `/api/admin/users/{id}` | Inspect, promote/suspend, or delete an account. |
| `GET` | `/api/admin/reports` | The moderation queue. |
| `POST` | `/api/admin/letters/{id}/moderate` | Hide or restore a public letter. |
| `GET` | `/api/admin/delivery/logs`, `/failed` | Delivery attempts and given-up letters. |
| `POST` | `/api/admin/delivery/{id}/retry` | Requeue a failed letter for the next pass. |
| `GET` `DELETE` | `/api/admin/suppressions` | Opted-out addresses; lift one. |

**The line this surface does not cross.** An admin can see that a user has
40 journal entries. An admin cannot see a title, an excerpt, or a body of
any of them — no route above selects `JournalEntry.body` or `.title`, and
`tests/test_admin.py` sweeps the whole surface to keep it that way. Public
letters *are* shown in the moderation queue, because they were published on
purpose and a moderator cannot judge a report without seeing what was
reported.

Guards worth knowing: an admin cannot change their own role or suspend
themselves, and the last active admin cannot be demoted by anyone — either
would lock the service out of its own admin surface. Hiding a letter takes
it off the public board but does **not** stop delivery; publishing was
optional, delivery is the promise. Deleting a user detaches their letters
rather than cancelling them, for the same reason.

### Health

| Path | Use |
|---|---|
| `/health` | Liveness. Static — answers 200 even if the database is down. |
| `/health/ready` | Readiness. Checks the database and that the scheduler is still advancing; 503 otherwise. |

Point your uptime monitor at **`/health/ready`** — it's the one that can
detect silently stalled delivery. Both answer `HEAD` as well as `GET`. Don't
poll it more than every 5 minutes: it runs a query on each call, which keeps
Neon's compute from ever autosuspending.

## Delivery

`check_and_send_letters()` runs hourly (`repeat_at(cron="0 * * * *")`), in
the web process. Each pass claims a batch of letters that are `scheduled`
with `deliver_at ≤ today`, sends them, and records the outcome. Failures
increment `attempts` and are retried on later passes until
`MAX_DELIVERY_ATTEMPTS`, after which the letter is marked `failed`.

Two consequences worth knowing:

- The pass only fires while the process is alive; there is no catch-up for
  hours missed while a free-tier host was asleep.
- Gmail caps sending at roughly 500 recipients/day and gives no bounce
  feedback, so a dead address just burns retries. It's fine for low volume
  and should be swapped for a real sending platform before real traffic.

## Testing

```bash
source .venv/bin/activate
pytest -v
```

Tests run against SQLite, so they need neither Postgres nor network access.

Two of them guard rules that are easy to break by accident and expensive to
notice later:

- `test_letters_have_no_edit_route` reads the route table and fails if a
  `PUT` or `PATCH` ever appears on a letter.
- `test_no_admin_route_returns_a_journal_entry` sweeps every admin endpoint
  for a fixture entry's title and body, so a new admin route that leaks
  private writing fails here rather than in production.

## Deployment

Deployed on FastAPI Cloud. Configuration lives in the platform's environment
variables, not in a file — `.env` is gitignored and never ships:

```bash
fastapi cloud env list
fastapi cloud env set FRONTEND_URL https://your-site
fastapi cloud env set SECRET_KEY <value> --secret   # --secret hides it from `env list`
fastapi deploy
```

Settings are read once at import and cached, so **changing an environment
variable requires a redeploy or restart** to take effect.

Checklist for the deployed environment:

- `FRONTEND_URL` is the real site URL, listed first
- `SECRET_KEY` is set, unique, and not the local one
- `SMTP_PORT=465`, and `SMTP_FROM_EMAIL` either unset or equal to `SMTP_USER`
  (Gmail rejects any other From address)
- `RUN_MIGRATIONS_ON_STARTUP=true`, or run `alembic upgrade head` yourself
- `MAILING_ADDRESS` filled in, for the CAN-SPAM footer and deliverability
- `DEBUG=false`

Rate limits are stored in-process, so they are per replica. Running more
than one worker multiplies the effective limit; point slowapi at Redis if
you scale out.

## Layout

```
backend/
├── app/
│   ├── main.py              # app, CORS, lifespan, scheduler task, Sentry
│   ├── config.py            # every setting, and the .env / .env.local layering
│   ├── database.py          # async engine and session factory
│   ├── models.py            # Letter, User, RefreshToken, Journal, JournalEntry, …
│   ├── schemas.py           # letter request/response models, email masking
│   ├── schemas_accounts.py  # account, journal, and admin models
│   ├── core/
│   │   ├── security.py      # capability tokens, HTML sanitising
│   │   ├── auth.py          # passwords, JWTs, sessions, route dependencies
│   │   ├── limiter.py       # slowapi setup
│   │   ├── logging.py       # log formatting
│   │   └── media.py         # upload validation
│   ├── routers/
│   │   ├── letters.py       # writing, confirming, cancelling, the public feed
│   │   ├── auth.py          # register, sign in, refresh, reset
│   │   ├── dashboard.py     # the signed-in home screen
│   │   ├── journals.py      # notebooks and entries
│   │   ├── admin.py         # stats, users, moderation, delivery health
│   │   └── health.py        # liveness and readiness
│   └── services/
│       ├── email_service.py # console / smtp / resend transports, templates
│       ├── account_email.py # verification and password-reset mail
│       └── scheduler.py     # hourly delivery pass
├── alembic/versions/        # migrations
├── scripts/run_scheduler.py # one delivery pass, on demand
└── tests/                   # pytest, SQLite
```

## License

MIT
