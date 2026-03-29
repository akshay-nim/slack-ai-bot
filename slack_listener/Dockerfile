# Use a specific Python 3.13.3 base image
FROM python:3.13.3-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 

WORKDIR /slack

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    dnsutils \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt
COPY . .

EXPOSE 5000
CMD ["python3", "slack_listener.py"]
