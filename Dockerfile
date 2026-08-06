FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY warden/ ./warden/
COPY templates/ ./templates/
COPY main.py .

# State and logs are mounted so history survives a container rebuild.
RUN mkdir -p /app/state /app/logs

ENV CONFIG_PATH=/app/config/config.json \
    STATE_PATH=/app/state/state.json \
    PYTHONUNBUFFERED=1

EXPOSE 3020

CMD ["python", "-u", "main.py"]
