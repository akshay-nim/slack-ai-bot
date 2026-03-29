# handlers.py
import re
import os
import logging
import hashlib
from abc import ABC, abstractmethod
from .kube_client import LogFetcher
from .kube_client import format_pod_status
from .summarizer import summarize_logs
from backend.exceptions import PodNotFoundError
from backend.loki_client import LokiClient
from .redis_client import RedisClient
from backend.config import REDIS_CACHE_TTL
from backend.metrics import FETCH_CACHE_HITS, FETCH_CACHE_MISSES

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# regex for suspicious secrets
SUSPICIOUS_PATTERNS = [
    re.compile(r"(AKIA|ASIA)[A-Z0-9]{16}"),   # AWS Access Keys
    re.compile(r"(?i)(ghp_[a-zA-Z0-9]{30,})"),  # GitHub personal token
    re.compile(r"-----BEGIN (RSA|DSA|EC|OPENSSH|PGP|PRIVATE|ENCRYPTED) PRIVATE KEY-----"),  # Private keys
    re.compile(r"ghp_[A-Za-z0-9]{36,}"),
    re.compile(r"gho_[A-Za-z0-9]{36,}"),
    re.compile(r"ghu_[A-Za-z0-9]{36,}"),
    re.compile(r"ghs_[A-Za-z0-9]{36,}"),
    re.compile(r"ghr_[A-Za-z0-9]{36,}"),
    re.compile(r"(?i)(eyJ[a-zA-Z0-9]{20,})"),   # Json web tokens
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),  # Slack tokens
    re.compile(r"-----BEGIN [A-Z ]+ PRIVATE KEY-----"),  # PEM private keys
    #re.compile(r"airflow", re.IGNORECASE)
]
class AlertHandler(ABC):
    def __init__(self, fetcher: LogFetcher):
        self.fetcher = fetcher

    @abstractmethod
    async def handle(self, payload: dict) -> dict:
        """
        Must return either {"logs": ...} or {"error": ...} or {"logs": ..., "summary": ...}.
        """
        pass

class PodCrashLoopingHandler(AlertHandler):
    def __init__(self, fetcher: LogFetcher):
        super().__init__(fetcher)
        self.loki_client = LokiClient()

    async def handle(self, payload: dict) -> dict:
        logger.info(f"Handling PodCrashLooping alert: {payload}")
        c         = payload["cluster"]
        ns        = payload["namespace"]
        pod       = payload["pod"]
        container = payload.get("container")
        tail      = payload.get("tail_lines", 300)
        used_loki = False
        try:
            raw_logs = await self.fetcher.fetch(c, ns, pod, container, tail)
        except PodNotFoundError:
            logger.info(f"Pod '{pod}' not found in namespace '{ns}'. Fetching logs from Loki.")
            try:
                raw_logs = await self.loki_client.fetch_logs(c, ns, pod, container, tail)
                if not raw_logs:
                    raise ValueError(f"No logs found for pod '{pod}' in cluster '{c}'")
                context_message = (
                    f"--- Original pod '{pod}' logs not found in Kubernetes cluster '{c}'. "
                    f"Logs fetched from Loki. ---\n"
                )
                raw_logs = raw_logs + context_message
                used_loki = True
            except Exception as e:
                test_grafana_url = f"https://grafana.central.test.lan.didevops.com/d/platform-sre-logs/logs"
                prod_grafana_url = f"https://grafana.central.adm.lan.didevops.com/d/platform-sre-logs/logs"
                return {
                    "error": f"Pod '{pod}' not found in cluster {c} and failed to fetch logs from Loki: {e}"
                             f"Check Grafana for logs: <{test_grafana_url}|Dev> or <{prod_grafana_url}|Prod>"
                }

        except Exception as e:
            return {"error": f"Error fetching logs: {e}"}

        # Check for secrets
        sanitized_lines = []
        suspicious_lines = []

        for line in raw_logs.splitlines():
            if any(pattern.search(line) for pattern in SUSPICIOUS_PATTERNS):
                suspicious_lines.append(line)
            else:
                sanitized_lines.append(line)

        sanitized_logs = "\n".join(sanitized_lines)

        if suspicious_lines:
            logger.debug(f"Suspicious lines removed from logs of pod '{pod}': {suspicious_lines}")

        #Build a Redis cache key purely from cluster:namespace:pod 
        cache_key = f"summarization:{c}:{ns}:{pod}"

        # Try to get a cached JSON summary for this pod 
        try:
            cached_json = await RedisClient.get(cache_key)
        except Exception as e:
            logger.warning(f"Redis GET error for key={cache_key}: {e}")
            cached_json = None

        if cached_json:
            FETCH_CACHE_HITS.labels(payload["alertname"]).inc()
            logger.info(f"Cache hit for pod {pod} (cluster={c}, ns={ns}).")
            return {
                "logs": sanitized_logs,
                "llm_summary": cached_json
            }
        
        # CACHE MISS
        FETCH_CACHE_MISSES.labels(payload["alertname"]).inc()
        logger.info(f"Cache miss for pod {pod} (cluster={c}, ns={ns}); calling LLM")
        
    # === Only fetch cluster data if pod was found in the cluster ===
        if not used_loki:
            try:
                prev_logs = await self.fetcher.fetch_previous_logs(c, ns, pod, container, tail) or ""
                events = await self.fetcher.fetch_pod_events(c, ns, pod)
                pod_obj = await self.fetcher.fetch_pod_description(c, ns, pod)
                pod_status_text = format_pod_status(pod_obj, container)
            except Exception as e:
                logger.warning(f"Failed to fetch additional pod details: {e}")
                prev_logs = ""
                events = ""
                pod_status_text = "N/A"
        else:
            prev_logs = ""
            events = ""
            pod_status_text = "N/A"


        # Combine all info and truncate to avoid token overflow
        context = f"""
    ==== Current Logs ====
    {sanitized_logs}

    ==== Previous Logs ====
    {prev_logs}

    ==== Pod Events ====
    {events}

    ==== All Pod Details ====
    {pod_status_text}
    """
        logger.info(f"Context of pod details for pod {pod}: {pod_status_text}")
        MAX_CHARS = 20000
        if len(context) > MAX_CHARS:
            context = context[-MAX_CHARS:]

        # Call the LLM for a new summary 
        try:
            summary_json = await summarize_logs(context)  # returns a JSON string
        except Exception as e:
            return {"error": f"Error summarizing logs: {e}"}

        # Store the new summary in Redis with a TTL 
        ttl_seconds = REDIS_CACHE_TTL
        logger.info(f"Storing summary in Redis cache with TTL={ttl_seconds}s for key={cache_key}: {summary_json}")
        try:
            await RedisClient.set(cache_key, summary_json, ex=ttl_seconds)
            logger.info(f"Stored summary in cache key={cache_key} with TTL={ttl_seconds}s.")
        except Exception as e:
            logger.warning(f"Redis SET error for key={cache_key}: {e}")

        # ── 9) Return the fresh summary 
        return {
            "logs": sanitized_logs,
            "llm_summary": summary_json
        }
