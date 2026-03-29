import aiohttp
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, List
from urllib.parse import urlencode
from backend.config import TEST_CLUSTERS, PROD_CLUSTERS, TEST_LOKI_URL, PROD_LOKI_URL

logger = logging.getLogger(__name__)

class LokiClient:
    def __init__(self):
        pass

    def get_loki_url_for_cluster(self, cluster: str) -> Optional[str]:
        logger.info(f"Getting Loki URL for cluster: {cluster}")
        if cluster in TEST_CLUSTERS:
            return TEST_LOKI_URL
        elif cluster in PROD_CLUSTERS:
            return PROD_LOKI_URL
        else:
            return None

    async def fetch_logs(self, cluster: str, namespace: str, pod_name: str, container: Optional[str] = None,
                         tail_lines: int = 500, start_time: Optional[int] = None, end_time: Optional[int] = None) -> str:
        
        loki_url = self.get_loki_url_for_cluster(cluster)
        if not loki_url:
            raise ValueError(f"Loki URL not configured for cluster '{cluster}'")

        logger.info(f"Fetching logs from Loki at '{loki_url}' for pod '{pod_name}' in namespace '{namespace}'")

        # Build the Loki log query
        log_query = f'{{namespace="{namespace}", pod="{pod_name}"}}'
        #if container:
        #    log_query += f', container="{container}"'
        #log_query += "}"

        # Use last 24 hours if no start/end provided
        now = datetime.now(timezone.utc)
        if not end_time:
            end_time = int(now.timestamp() * 1e9)  # nanoseconds
        if not start_time:
            start = now - timedelta(hours=24)  
            start_time = int(start.timestamp() * 1e9)  # nanoseconds

        params = {
            "query": log_query,
            "limit": 500,
            "start": start_time,
            "end": end_time,
            "direction": "BACKWARD"  # Get recent logs backwards
        }

        async with aiohttp.ClientSession() as session:
            try:
                logger.info(f"Querying Loki with params: {params}")
                async with session.get(f"{loki_url}/loki/api/v1/query_range", params=params) as response:
                    if response.status != 200:
                        text = await response.text()
                        raise Exception(f"Loki query failed with status {response.status}: {text}")

                    data = await response.json()
                    # Loki returns logs as streams of entries, we must parse them
                    logs = []
                    streams = data.get("data", {}).get("result", [])
                    for stream in streams:
                        for entry in stream.get("values", []):
                            # entry is [timestamp, log_line]
                            logs.append(entry[1])
                    return "\n".join(logs)
            except Exception as e:
                logger.error(f"Error fetching logs from Loki: {e}")
                raise
