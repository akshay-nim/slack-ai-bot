# redis_client.py

import os
import logging

from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)
from redis.asyncio import Redis
from redis.asyncio.sentinel import Sentinel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class RedisClient:
    """
    Singleton wrapper around a Redis client obtained via Redis Sentinel.
    All get/set operations are retried up to 3 times on exception
    (e.g., connection errors), with exponential backoff.
    """

    _instance: Redis | None = None

    @classmethod
    def _get_socket_timeout(cls) -> float:
        """
        Read the socket timeout (in seconds) from CACHE_REDIS_SOCKET_TIMEOUT.
        Defaults to 5.0 seconds if not set or invalid.
        """
        try:
            return float(os.getenv("CACHE_REDIS_SOCKET_TIMEOUT", "10.0"))
        except ValueError:
            return 10.0

    @classmethod
    def _create_sentinel_client(cls) -> Redis:
        """
        Create a Sentinel-backed Redis connection. Expects these env vars:
          • CACHE_REDIS_SENTINEL_URL    (sentinel host, e.g., "dev-redis.lan.di4.us")
          • CACHE_REDIS_SENTINEL_PORT   (sentinel port, e.g., "26380")
          • CACHE_REDIS_SENTINEL_MASTER (master name, e.g., "mymaster")
          • CACHE_REDIS_PASSWORD        (password for the Redis master; required if master enforces AUTH)
          • CACHE_REDIS_SOCKET_TIMEOUT  (socket timeout in seconds; optional)
        """
        sentinel_host = os.getenv("CACHE_REDIS_SENTINEL_URL")
        sentinel_port = int(os.getenv("CACHE_REDIS_SENTINEL_PORT"))
        master_name   = os.getenv("CACHE_REDIS_SENTINEL_MASTER")
        password      = os.getenv("CACHE_REDIS_PASSWORD", "") or None
        timeout       = cls._get_socket_timeout()

        if not sentinel_host or not master_name:
            raise ValueError(
                "When using Sentinel mode, you must set both "
                "`CACHE_REDIS_SENTINEL_URL` and `CACHE_REDIS_SENTINEL_MASTER`."
            )

        logger.info(
            f"Connecting to Redis Sentinel at {sentinel_host}:{sentinel_port}, "
            f"master_name={master_name}, socket_timeout={timeout}s"
        )

        # Connect to Sentinel without a password (Sentinel itself does not require AUTH)
        sentinel = Sentinel(
            [(sentinel_host, sentinel_port)],
            socket_timeout=timeout
        )

        # When asking for the master, supply the master’s password if provided
        if password:
            redis_master: Redis = sentinel.master_for(
                service_name=master_name,
                password=password,
                socket_timeout=timeout,
            )
        else:
            # If no password is set, assume the master does not require AUTH
            redis_master: Redis = sentinel.master_for(
                service_name=master_name,
                socket_timeout=timeout,
            )

        return redis_master

    @classmethod
    def get_redis(cls) -> Redis:
        """
        Return a singleton Redis instance connected via Sentinel.
        """
        if cls._instance is None:
            cls._instance = cls._create_sentinel_client()
            logger.info("Initialized Sentinel-backed Redis client")
        return cls._instance

    @classmethod
    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, max=10),
        retry=retry_if_exception_type(Exception),
    )
    async def get(cls, key: str) -> str | None:
        """
        Async wrapper around redis.get(key). Retries up to 3 times on any exception.
        """
        r = cls.get_redis()
        try:
            logger.debug(f"Attempting to GET key: {key}")
            return await r.get(key)
        except Exception as e:
            logger.warning(f"Redis GET failed (key={key}): {e!r}. Retrying...")
            raise

    @classmethod
    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, max=10),
        retry=retry_if_exception_type(Exception),
    )
    async def set(cls, key: str, value: str, ex: int | None = None) -> None:
        """
        Async wrapper around redis.set(key, value, ex=...). Retries up to 3 times on any exception.
        """
        r = cls.get_redis()
        try:
            logger.info(f"Attempting to SET key: {key} with value: {value} (ex={ex})")
            await r.set(key, value, ex=ex)
        except Exception as e:
            logger.warning(f"Redis SET failed (key={key}): {e!r}. Retrying...")
            raise
