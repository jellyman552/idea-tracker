FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tracker ./tracker
COPY migrations ./migrations

# instance/ holds the SQLite database; mount a volume there to keep your data.
RUN useradd --create-home --uid 1000 app && mkdir -p instance && chown app instance
USER app

ENV PORT=8000
EXPOSE 8000
# Apply migrations, create the login user from ADMIN_USERNAME/ADMIN_PASSWORD if
# set, then serve the web app (and the MCP server if MCP_TOKEN is set).
# No access log: the MCP path contains the secret token.
CMD ["sh", "-c", "flask --app tracker db upgrade && flask --app tracker ensure-user && exec uvicorn --factory tracker.asgi:create --host 0.0.0.0 --port ${PORT} --no-access-log --proxy-headers --forwarded-allow-ips='*'"]
