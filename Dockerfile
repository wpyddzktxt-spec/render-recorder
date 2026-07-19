FROM python:3.12-slim-bookworm
# Render detects this as Docker runtime; install ffmpeg + python deps
RUN apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends ffmpeg curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY monitor.py server.py ./
EXPOSE 10000
CMD ["python", "monitor.py"]
