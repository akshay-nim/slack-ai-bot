# backend/metrics.py

from prometheus_client import Counter

# counter for how many requests were served directly from Redis cache
FETCH_CACHE_HITS = Counter(
    "fetch_cache_hits_total",
    "Total number of fetch-logs requests served from Redis cache",
    ["alertname"],
)

# counter for how many requests missed the cache 
FETCH_CACHE_MISSES = Counter(
    "fetch_cache_misses_total",
    "Total number of fetch-logs requests that missed the Redis cache",
    ["alertname"],
)

# Count how many prompt tokens we send to the model
LLM_PROMPT_TOKENS = Counter(
    "llm_prompt_tokens_total",
    "Total number of prompt tokens sent to the LLM",
    ["model"],
)

# Count how many completion tokens the model returns
LLM_COMPLETION_TOKENS = Counter(
    "llm_completion_tokens_total",
    "Total number of completion tokens returned by the LLM",
    ["model"],
)

LLM_TOTAL_TOKENS = Counter(
    "llm_total_tokens_total",
    "Total number of tokens (prompt + completion) used",
    ["model"],
)