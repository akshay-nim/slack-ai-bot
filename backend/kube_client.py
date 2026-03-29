# kube_client.py
import os
import asyncio
import logging
from typing import Optional
from cachetools import TTLCache
from kubernetes_asyncio import client as k8s_client, config as k8s_config
from kubernetes_asyncio.client.rest import ApiException
from backend.config import KUBECONFIG_MAP, DEFAULT_TAIL, CLIENT_TTL
from backend.exceptions import PodNotFoundError, KubeconfigNotFoundError

# Enable logging for Kubernetes calls
logging.getLogger("kubernetes_asyncio").setLevel(logging.INFO)
logging.getLogger("urllib3").setLevel(logging.INFO)

logger = logging.getLogger(__name__)

# Utility function to check file existence and readability
def is_file_readable(file_path: str) -> bool:
    return os.path.isfile(file_path) and os.access(file_path, os.R_OK)

class KubeClientManager:
    """Caches a CoreV1Api per cluster for CLIENT_TTL seconds."""
    
    def __init__(self, cache_size: int = 10):
        self._cache: TTLCache[str, k8s_client.CoreV1Api] = TTLCache(maxsize=cache_size, ttl=CLIENT_TTL)
        self._lock = asyncio.Lock()

    async def load_client(self, cluster: str) -> k8s_client.CoreV1Api:  
        if cluster not in KUBECONFIG_MAP:
            raise KeyError(f"No kubeconfig for cluster '{cluster}'")
        
        kubeconfig_path = KUBECONFIG_MAP[cluster]
        if not is_file_readable(kubeconfig_path):
            raise KubeconfigNotFoundError(f"Kubeconfig file not found or unreadable: '{kubeconfig_path}'")

        await k8s_config.load_kube_config(config_file=kubeconfig_path)
        return k8s_client.CoreV1Api()

    async def get_client(self, cluster: str) -> k8s_client.CoreV1Api:
        if cluster not in self._cache:
            async with self._lock:
                if cluster not in self._cache:
                    # Changed: replaced direct load with load_client()
                    client = await self.load_client(cluster)  # <<< CHANGED
                    self._cache[cluster] = client
        return self._cache[cluster]

    # Added this method to forcibly refresh client on 401 errors
    async def refresh_client(self, cluster: str) -> k8s_client.CoreV1Api:  
        async with self._lock:
            client = await self.load_client(cluster)
            self._cache[cluster] = client
            logger.info(f"Refreshed Kubernetes client for cluster '{cluster}' due to Unauthorized error.")  # <<< ADDED
            return client  

class LogFetcher:
    """Fetches pod logs, raising PodNotFoundError on 404 errors."""
    
    def __init__(self, manager: KubeClientManager):
        self.manager = manager

    async def fetch(
        self, cluster: str, namespace: str, pod: str, container: Optional[str] = None, tail_lines: Optional[int] = None
    ) -> str:
        async def try_fetch(client):  
            logger.info(f"Fetching logs for pod '{pod}' in namespace '{namespace}' on cluster '{cluster}' with container '{container}'")
            pod_obj = await client.read_namespaced_pod(name=pod, namespace=namespace)
            containers = [c.name for c in pod_obj.spec.containers]
            chosen_container = container if container in containers else containers[0]
            lines = tail_lines or DEFAULT_TAIL
            stream = await client.read_namespaced_pod_log(
                name=pod, namespace=namespace, container=chosen_container, tail_lines=lines, _preload_content=False
            )
            data = await stream.read()
            return data.decode("utf-8", errors="replace")

        client = await self.manager.get_client(cluster)
        try:
            return await try_fetch(client)

        except ApiException as e:
            logger.error("Error reading pod '%s/%s': status=%s, reason=%s, body=%s", namespace, pod, e.status, e.reason, e.body)
            if e.status == 404:
                raise PodNotFoundError(f"Pod '{pod}' not found in namespace '{namespace}'")

            # === BEGIN ADDED: Retry on 401 Unauthorized ===
            if e.status == 401:  
                logger.warning(f"Received 401 Unauthorized for cluster '{cluster}', refreshing client and retrying...")  # <<< ADDED
                client = await self.manager.refresh_client(cluster)  
                try:
                    return await try_fetch(client)  
                except ApiException as e2:
                    logger.error("Retry after client refresh failed: %s", e2) 
                    if e2.status == 404:
                        raise PodNotFoundError(f"Pod '{pod}' not found in namespace '{namespace}'")
                    raise e2
            raise

    async def find_replacement_pods(self, cluster: str, namespace: str, pod_name: str) -> list[str]:
        """
        Find replacement pods for the given pod_name in the specified namespace.
        Uses prefix matching on pod name by removing the last hyphen-separated suffix.
        Returns a list of pod names matching the prefix, sorted by creation time descending.
        """

        def get_base_name(pod_name: str) -> str:
            if '-' in pod_name:
                return pod_name.rsplit('-', 1)[0]
            else:
                return pod_name
      
        base_name = get_base_name(pod_name)
        logger.info(f"finding base name:  '{base_name}'")

        client = await self.manager.get_client(cluster)
        try:
            pods = await client.list_namespaced_pod(namespace=namespace)
        except ApiException as e:
            logger.error(f"Failed to list pods in {namespace} on cluster {cluster}: {e}")
            return []

        candidates = [
            pod for pod in pods.items
            if pod.metadata.name.startswith(base_name)
        ]
        candidates.sort(key=lambda p: p.metadata.creation_timestamp, reverse=True)
        logger.info(f"Found {len(candidates)} replacement pods for '{pod_name}' in namespace '{namespace}' on cluster '{cluster}'")
        return [pod.metadata.name for pod in candidates]


    async def fetch_previous_logs(
        self, 
        cluster: str, 
        namespace: str, 
        pod: str, 
        container: Optional[str] = None, 
        tail_lines: Optional[int] = None
    ) -> Optional[str]:
        api = await self.manager.get_client(cluster)
        pod_obj = await api.read_namespaced_pod(name=pod, namespace=namespace)
        containers = [c.name for c in pod_obj.spec.containers]
        chosen_container = container if container in containers else containers[0]
        logger.info(f"Fetching previous logs for pod '{pod}' in namespace '{namespace}' on cluster '{cluster}' with container '{chosen_container}'")
        try:
            stream = await api.read_namespaced_pod_log(
                name=pod, 
                namespace=namespace, 
                container=chosen_container, 
                tail_lines=tail_lines or DEFAULT_TAIL, 
                previous=True, 
                _preload_content=False
            )
            data = await stream.read()
            return data.decode("utf-8", errors="replace")
        except ApiException as e:
            logger.warning("No previous logs available for pod '%s/%s': %s", namespace, pod, e)
            return None

    async def fetch_pod_events(
        self, 
        cluster: str, 
        namespace: str, 
        pod: str
    ) -> str:
        api = await self.manager.get_client(cluster)
        try:
            field_selector = f"involvedObject.name={pod}"
            logger.info(f"Fetching events for pod '{pod}' in namespace '{namespace}' on cluster '{cluster}'")
            events = await api.list_namespaced_event(namespace=namespace, field_selector=field_selector)
            return "\n".join(f"{evt.type}: {evt.reason} - {evt.message}" for evt in events.items)
        except ApiException as e:
            logger.warning("Failed to fetch events for pod '%s/%s': status=%s, reason=%s, body=%s", namespace, pod, e.status, e.reason, e.body)
            return ""

    async def fetch_pod_description(self, cluster: str, namespace: str, pod: str):
        """
        Fetch the full Pod object from the given cluster, namespace, and pod name.
        """
        api = await self.manager.get_client(cluster)
        try:
            logger.info(f"Fetching pod description for '{pod}' in namespace '{namespace}' on cluster '{cluster}'")
            pod_obj = await api.read_namespaced_pod(name=pod, namespace=namespace)
            return pod_obj
      
        except ApiException as e:
            logger.warning("Error fetching pod description for '%s/%s': status=%s, reason=%s, body=%s", namespace, pod, e.status, e.reason, e.body)
            return ""

# Functions :

def format_pod_status(pod_obj, container=None) -> str:
    """
    Extracts and formats relevant pod status details for LLM consumption.
    Excludes env, secrets, and annotations.
    """
    status = pod_obj.status
    metadata = pod_obj.metadata
    spec = pod_obj.spec

    lines = []
    lines.append(f"Pod Name: {metadata.name}")
    lines.append(f"Namespace: {metadata.namespace}")
    lines.append(f"Node: {spec.node_name}")
    lines.append(f"Pod Phase: {status.phase}")

    if status.reason:
        lines.append(f"Reason: {status.reason}")
    if status.message:
        lines.append(f"Message: {status.message}")

    # Pod-level conditions
    if status.conditions:
        lines.append("Conditions:")
        for cond in status.conditions:
            lines.append(f"  - {cond.type}: {cond.status} (Reason: {cond.reason}, Message: {cond.message})")

    # Container-level status
    def process_status(name, cs):
        cur_state = cs.state
        last_state = cs.last_state
        status_line = f"{name}:"
        if cur_state.waiting:
            status_line += f" Current=Waiting ({cur_state.waiting.reason})"
        elif cur_state.running:
            status_line += f" Current=Running"
        elif cur_state.terminated:
            status_line += f" Current=Terminated ({cur_state.terminated.reason}, Exit Code: {cur_state.terminated.exit_code})"

        if last_state and last_state.terminated:
            status_line += f" | Last=Terminated ({last_state.terminated.reason}, Exit Code: {last_state.terminated.exit_code})"
        return status_line

    lines.append("Container Statuses:")
    for cs in status.init_container_statuses or []:
        lines.append("  Init " + process_status(cs.name, cs))
    for cs in status.container_statuses or []:
        if container and cs.name != container:
            continue
        lines.append("  " + process_status(cs.name, cs))

    return "\n".join(lines)


# Validate that critical configurations are set correctly at startup
def validate_config():
    if not isinstance(KUBECONFIG_MAP, dict) or not KUBECONFIG_MAP:
        logging.error("KUBECONFIG_MAP is not configured properly.")
        raise ValueError("KUBECONFIG_MAP is not configured properly.")
    
    if not isinstance(DEFAULT_TAIL, int) or DEFAULT_TAIL <= 0:
        logging.error("DEFAULT_TAIL must be a positive integer.")
        raise ValueError("DEFAULT_TAIL must be a positive integer.")

    if not isinstance(CLIENT_TTL, int) or CLIENT_TTL <= 0:
        logging.error("CLIENT_TTL must be a positive integer.")
        raise ValueError("CLIENT_TTL must be a positive integer.")

# Call the configuration validation when the module is loaded
validate_config()
