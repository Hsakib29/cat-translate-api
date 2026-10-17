FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY worker.py ./

RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 10000
# One worker on purpose: the "one request at a time" queue lives inside this process.
CMD exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port ${PORT:-10000} --workers 1
