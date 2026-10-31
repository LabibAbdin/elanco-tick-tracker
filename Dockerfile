FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# dependencies first, so code changes don't invalidate the layer
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY tools ./tools
COPY data/snapshot ./data/snapshot

# The database lives in /srv/var (mount a volume there); the bundled snapshot stays in the
# image under /srv/data so a volume never hides or freezes it.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /srv/var \
    && chown appuser /srv/var
USER appuser

ENV DATABASE_URL=sqlite:////srv/var/ticks.db
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
