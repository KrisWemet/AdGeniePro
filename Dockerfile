FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN useradd --create-home --uid 10001 adgenie

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chown -R adgenie:adgenie /app
USER adgenie

EXPOSE 8000

CMD ["sh", "-c", "uvicorn adgenie.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
