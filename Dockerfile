# Price-tracker: a tiny always-on polling loop. Runs `python run.py` forever.
# Secrets (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) are provided as environment
# variables by the host (e.g. Railway) — no .env file is baked into the image.
FROM python:3.12-slim

# Unbuffered stdout so logs stream to the platform in real time.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install deps first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code (see .dockerignore for what's excluded — .env, *.db, .venv, etc.).
COPY . .

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app
USER appuser

CMD ["python", "run.py"]
