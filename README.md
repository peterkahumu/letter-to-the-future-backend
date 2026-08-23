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
  address. Always set it in production.

## API

All letter routes are under `/api/letters`.

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
│   ├── models.py            # Letter, EmailSuppression, LetterReport, DeliveryLog
│   ├── schemas.py           # request/response models, email masking
│   ├── core/
│   │   ├── security.py      # capability tokens, HTML sanitising
│   │   ├── limiter.py       # slowapi setup
│   │   ├── logging.py       # log formatting
│   │   └── media.py         # upload validation
│   ├── routers/
│   │   ├── letters.py       # the API above
│   │   └── health.py        # liveness and readiness
│   └── services/
│       ├── email_service.py # console / smtp / resend transports, templates
│       └── scheduler.py     # hourly delivery pass
├── alembic/versions/        # migrations
├── scripts/run_scheduler.py # one delivery pass, on demand
└── tests/                   # pytest, SQLite
```

## License

MIT
