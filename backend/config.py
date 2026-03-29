#config.py

import os
from typing import Dict

KUBECONFIG_MAP: Dict[str, str] = {
    "test1"           : "/tmp/test1",
    "test2"           : "/tmp/test2",
    "prod1"           : "/tmp/prod1",
    "prod2"           : "/tmp/prod2"
}

TEST_CLUSTERS   = ["test1", "test2"]
PROD_CLUSTERS   = ["prod1", "prod2"]
TEST_LOKI_URL   = ""
PROD_LOKI_URL   = ""
LOG_LEVEL       = "INFO"
DEFAULT_TAIL    = int(os.getenv("TAIL_LINES", "300"))
CLIENT_TTL      = int(os.getenv("CLIENT_CACHE_TTL", "60"))
REDIS_CACHE_TTL = int(os.getenv("REDIS_CACHE_TTL", "7200"))
