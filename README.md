# Letter to the Future - Backend ✉️

This is the backend API for the "Letter to the Future" application. It provides endpoints for creating letters and a background scheduler that sends them on the chosen date.

**Frontend Repository:** [letter-to-the-future-frontend](https://github.com/peterkahumu/letter-to-the-future-frontend)

## Technology Stack
- FastAPI
- SQLAlchemy (async)
- PostgreSQL (via Neon)
- Python `smtplib` + Gmail SMTP for emails

## Getting Started

### Local Development

1. Create a virtual environment and install dependencies:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. Copy and configure the environment:
   ```bash
   cp .env.example .env
   # Edit .env with your DATABASE_URL, SMTP credentials, etc.
   ```
3. Run database migrations:
   ```bash
   alembic upgrade head
   ```
4. Start the server:
   ```bash
   uvicorn app.main:app --reload --port 8000
   ```

## Docker Compose

If you'd like to run both the frontend and backend together using Docker, you can use the following `docker-compose.yml` configuration (assuming both repositories are cloned in the same parent directory):

<details>
<summary>Click to expand <code>docker-compose.yml</code></summary>

```yaml
services:
  backend:
    build:
      context: ./backend
      dockerfile: Dockerfile
    container_name: lttf-backend
    ports:
      - "8000:8000"
    env_file:
      - ./backend/.env
    healthcheck:
      test: [ "CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" ]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s
    restart: unless-stopped

  frontend:
    build:
      context: ./frontend
      dockerfile: Dockerfile
    container_name: lttf-frontend
    ports:
      - "3000:3000"
    env_file:
      - ./frontend/.env.local
    depends_on:
      backend:
        condition: service_healthy
    restart: unless-stopped
```

</details>
