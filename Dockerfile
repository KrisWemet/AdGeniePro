FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --create-home adgenie \
    && mkdir /data && chown adgenie:adgenie /data
COPY --chown=adgenie:adgenie adgenie ./adgenie
USER adgenie
EXPOSE 8000
# PORT is set by most hosts; 8000 is the Compose default.
ENV PORT=8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT','8000')}/healthz\", timeout=4)"
CMD ["sh", "-c", "exec uvicorn adgenie.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
