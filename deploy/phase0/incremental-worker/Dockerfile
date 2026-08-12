FROM python:3.12-slim

WORKDIR /app
COPY deploy/phase0/incremental-worker/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY ingestion /app/ingestion
COPY chat_logs /app/chat_logs
COPY deploy/phase0/incremental-worker/app.py /app/app.py

ENV PYTHONUNBUFFERED=1
EXPOSE 8003
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8003"]
