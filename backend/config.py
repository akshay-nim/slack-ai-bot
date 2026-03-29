#config.py

import os
from typing import Dict

KUBECONFIG_MAP: Dict[str, str] = {
    "a.test.didevops.com"      : "/tmp/dataflow-connector_a-test",
    "b.test.didevops.com"      : "/tmp/dataflow-connector_b-test",
    "a.prod.di11.us"           : "/tmp/slack-ai-bot_a-prod-di11",
    "b.prod.di11.us"           : "/tmp/slack-ai-bot_b-prod-di11",
    "c.prod.di11.us"           : "/tmp/slack-ai-bot_c-prod-di11",
    "central.di11.didevops.com": "/tmp/slack-ai-bot_central-di11"
}

TEST_CLUSTERS   = ["a.test.didevops.com", "b.test.didevops.com"]
PROD_CLUSTERS   = ["a.prod.di11.us", "b.prod.di11.us", "c.prod.di11.us", "central.di11.didevops.com"]
TEST_LOKI_URL   = "https://loki.central.test.lan.didevops.com"
PROD_LOKI_URL   = "https://loki.central.di11.lan.didevops.com"
LOG_LEVEL       = "INFO"
DEFAULT_TAIL    = int(os.getenv("TAIL_LINES", "300"))
CLIENT_TTL      = int(os.getenv("CLIENT_CACHE_TTL", "60"))
REDIS_CACHE_TTL = int(os.getenv("REDIS_CACHE_TTL", "7200"))