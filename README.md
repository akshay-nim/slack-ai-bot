# Slack AI Bot for kubernetes resource Issues

A Slack AI Bot for comprehensive alert triage and log analysis in Kubernetes environments.
It consists of two main components:

1. **slack_listener**: A Slack Bolt-based service that listens for alert messages in Slack,
   forwards alert context to a backend AI/log analysis service, and posts rich summaries and log files back to users.
2. **backend**: A FastAPI service that fetches logs from Kubernetes clusters or Loki, summarizes logs
   using OpenAI LLM, caches summaries in Redis, and exposes metrics via Prometheus.

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Components](#components)
- [Getting Started](#getting-started)
  - [Prerequisites](#prerequisites)
  - [Configuration](#configuration)
  - [Running](#running)
- [Usage](#usage)
- [Project Structure](#project-structure)
- [Contributing](#contributing)

## Features

- Alert filtering and parsing for Kubernetes pod failures (e.g., PodCrashLooping, PodInErrorState)
- AI-driven log summarization using OpenAI LLM with custom JSON output
- Fetch logs from Kubernetes API or Loki as a fallback
- Redis-backed caching of summaries for fast repeat access
- Rich Slack messages with analysis, key error lines, summaries, suggestions, and log download buttons
- Robust retry and exponential backoff for backend calls and Redis operations
- Prometheus metrics for monitoring request counts, cache hits/misses, and token usage

## Architecture

High-level architecture of the system:

```mermaid
flowchart TD
  Slack[Slack Workspace]                                   
  SlackListener[slack_listener Service]                    
  Backend[Backend Service (FastAPI)]                       
  K8s[Kubernetes Clusters]                                 
  Loki[Loki Logging]                                       
  OpenAI[OpenAI LLM]                                       
  Redis[Redis Sentinel]                                    
  Prometheus[Prometheus]                                   

  Slack      -->|Alert Message| SlackListener  
  SlackListener -->|HTTP POST /fetch-logs| Backend
  Backend    -->|Kubernetes API| K8s
  Backend    -->|Loki API| Loki
  Backend    -->|OpenAI API| OpenAI
  Backend    -->|Cache| Redis
  Backend    -->|Metrics| Prometheus
  Backend    -->|Response| SlackListener
  SlackListener -->|Slack API| Slack


## Components

### slack_listener

Implements the Slack Bolt listener that:
- Connects to Slack via Socket Mode
- Filters and parses incoming alert messages
- Sends fetch requests to the backend service
- Posts analysis summaries and provides a "View Logs" button
- Handles interactive button clicks to fetch and upload full logs

Key files:
- `slack_listener/slack_listener.py`: Main event handlers and business logic
- `slack_listener/teams-services.json`: Mapping of pod name patterns to Slack subteam IDs
- `slack_listener/Dockerfile`, `slack_listener/requirements.txt`

### backend

Provides a FastAPI-based service that:
- Exposes `/fetch-logs` POST endpoint for alert-based log fetching and summarization
- Fetches logs via Kubernetes API (`kube_client`) or Loki (`loki_client`) as needed
- Performs LLM-driven log summarization (`summarizer`) with strict JSON output
- Caches summaries in Redis Sentinel (`redis_client`)
- Tracks Prometheus metrics (`metrics`)

Key files:
- `backend/main.py`: FastAPI app and endpoint definitions
- `backend/config.py`: Kubeconfig mappings and default settings
- `backend/handlers.py`: Alert-specific handlers and summarization workflow
- `backend/kube_client.py`, `backend/loki_client.py`, `backend/redis_client.py`, `backend/summarizer.py`
- `backend/requirements.txt`, `backend/Dockerfile`

## Getting Started

### Prerequisites

- Python 3.8+ (for local development)
- Docker & Docker Compose (optional)
- Kubernetes kubeconfig files for target clusters (as configured in `backend/config.py`)
- Slack App credentials with Bot Token, App Token, and Signing Secret
- OpenAI API key for LLM access
- Redis Sentinel deployment for caching summaries

### Configuration

#### Slack Listener (`/tmp/.slackaibotenv`)

Create an environment file with:

```ini
slack_bot_token=YOUR_SLACK_BOT_TOKEN
slack_signing_secret=YOUR_SLACK_SIGNING_SECRET
slack_app_token=YOUR_SLACK_APP_TOKEN
override_post_channel=            # Optional: Slack channel ID to override posting
default_backend_url=http://backend:5000/fetch-logs
tracked_alertnames=PodCrashLooping,PodInErrorState,PodPendingTooLong
log_level=INFO
retry_attempts=2
retry_backoff_base=1.0
retry_backoff_max=10.0
```

#### Backend (Environment Variables)

```ini
OPENAI_API_KEY=YOUR_OPENAI_API_KEY
CACHE_REDIS_SENTINEL_URL=your-sentinel-host
CACHE_REDIS_SENTINEL_PORT=26380
CACHE_REDIS_SENTINEL_MASTER=mymaster
CACHE_REDIS_PASSWORD=      # Optional if Redis requires AUTH
```

### Running

#### Local Development (Python)

```bash
# Backend
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 5000

# Slack Listener
cd ../slack_listener
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
export $(xargs < /tmp/.slackaibotenv)
python slack_listener.py
```

#### Docker

```bash
# Build images
docker build -t slack-ai-backend -f backend/Dockerfile .
docker build -t slack-ai-listener -f slack_listener/Dockerfile .

# Run services (customize environment and network as needed)
docker network create slack-ai-net
docker run -d --name redis-sentinel --network slack-ai-net your-redis-sentinel-image
docker run -d --name backend --network slack-ai-net -e OPENAI_API_KEY=... slack-ai-backend
docker run -d --name listener --network slack-ai-net -e slack_bot_token=... -e slack_signing_secret=... -e slack_app_token=... slack-ai-listener
```

## Usage

Once both services are running and the Slack App is installed:
1. Trigger a Kubernetes alert in Slack (e.g., via Prometheus Alertmanager).
2. The bot will automatically respond with an AI-driven analysis in thread.
3. Click **View Logs** to fetch and upload full pod logs.

## Project Structure

```text
.
├── backend
│   ├── main.py
│   ├── config.py
│   ├── handlers.py
│   ├── kube_client.py
│   ├── loki_client.py
│   ├── summarizer.py
│   ├── redis_client.py
│   ├── exceptions.py
│   ├── metrics.py
│   ├── requirements.txt
│   └── Dockerfile
├── slack_listener
│   ├── slack_listener.py
│   ├── teams-services.json
│   ├── requirements.txt
│   └── Dockerfile
└── README.md
```

## Contributing

Contributions are welcome! Please open issues or pull requests for bugs, improvements,
or new alert handlers. Ensure to follow the existing code style and include tests
for new functionality when possible.

## Future Improvements

Below are some ideas for future enhancements, architectural optimizations, and scalability improvements.

### AI/ML Enhancements
- Fine-tune an open-source LLM or host an on-prem model for lower latency, cost control, and data privacy.
- Add anomaly detection and root cause analysis using time-series or log-based ML algorithms.

### Extensibility & Customization
- Develop a plugin/handler framework to allow adding custom alert processors without modifying core code.
- Introduce configuration as code (e.g., Terraform, Pulumi) for infrastructure provisioning.

