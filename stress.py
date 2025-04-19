#!/usr/bin/env python3
"""
Sophisticated Chaos Engineering Stress Tester with Adaptive Throttling,
Prometheus Telemetry, Redis Caching, Circuit Breaking, Socket-Level Optimizations,
In-Script Profiling, and Kubernetes Chaos Integration.

This script generates configurable high-concurrency HTTP load, monitors errors,
adapts request rates, caches GET responses, breaks circuits on repeated failures,
and periodically terminates Kubernetes pods to inject chaos.
"""

import os
import time
import random
import logging
import socket
import threading
import signal
import sys
import statistics
import collections
from typing import List, Optional, Deque, NamedTuple, Tuple
import requests
import redis
import pybreaker
import kubernetes.client
from kubernetes import config as kube_config
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from prometheus_client import start_http_server, Counter, Histogram, Gauge
from urllib3.exceptions import NewConnectionError
import concurrent.futures
from subprocess import Popen

# --- Configuration ---
TARGET_URL = os.getenv('TARGET_URL', 'http://localhost:8000')
NUM_REQUESTS = int(os.getenv('NUM_REQUESTS', '5000'))
NUM_WORKERS = int(os.getenv('NUM_WORKERS', '100'))

REQUEST_METHOD = 'GET'
REQUEST_HEADERS = {
    'User-Agent': 'EnhancedPythonStressTester/1.0',
    'Accept': '*/*',
}
CONNECT_TIMEOUT = float(os.getenv('CONNECT_TIMEOUT', '5.0'))
READ_TIMEOUT = float(os.getenv('READ_TIMEOUT', '15.0'))
ALLOW_REDIRECTS = True
VERIFY_SSL = True
STREAM_RESPONSE = False

ENABLE_RETRIES = True
MAX_RETRIES = 3
RETRY_BACKOFF_FACTOR = 0.5
RETRY_ON_STATUS_CODES = {500, 502, 503, 504}

ENABLE_ADAPTIVE_THROTTLING = True
ERROR_THRESHOLD = 0.05
THROTTLING_WINDOW_SIZE = 200
BACKOFF_WINDOW_BASE = 0.1
BACKOFF_WINDOW_MAX = 10.0

# Redis for caching & circuit breaker state
REDIS_HOST = os.getenv('REDIS_HOST', 'redis-cache')
REDIS_PORT = int(os.getenv('REDIS_PORT', '6379'))
REDIS_DB = 0
CACHE_TTL = 300  # seconds
REDIS_CLIENT = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB)
CIRCUIT_BREAKER = pybreaker.CircuitBreaker(
    fail_max=5,
    reset_timeout=30,
    state_storage=pybreaker.CircuitBreakerRedisStorage(REDIS_CLIENT)
)

# Prometheus metrics
REQUEST_COUNTER = Counter('http_requests_total', 'Total HTTP requests', ['method','status','endpoint'])
LATENCY_HISTOGRAM = Histogram('http_request_duration_seconds','Request latency', ['endpoint'])
ERROR_GAUGE      = Gauge('consecutive_errors','Consecutive error streak')
TOKEN_BUCKET_GAUGE = Gauge('throttle_tokens','Available leaky bucket tokens')

# Kubernetes chaos settings
CHAOS_NAMESPACE = os.getenv('CHAOS_NAMESPACE', 'production')
CHAOS_LABEL_SELECTOR = os.getenv('CHAOS_LABEL_SELECTOR', 'app=critical-service')
CHAOS_INTERVAL_MIN = 30
CHAOS_INTERVAL_MAX = 120

# Analysis percentiles
LATENCY_PERCENTILES = [50, 75, 90, 95, 99, 99.9]

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(threadName)s] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Shared state
results_store: Deque['RequestResult'] = collections.deque()
throttling_error_window: Deque[bool] = collections.deque(maxlen=THROTTLING_WINDOW_SIZE)
thread_local = threading.local()
shutdown_event = threading.Event()
active_futures: List[concurrent.futures.Future] = []
results_lock = threading.Lock()

class RequestResult(NamedTuple):
    status_code: Optional[int]
    latency_ms: Optional[float]
    success: bool
    error_type: Optional[str]
    thread_name: str
    timestamp: float
    content_length: int = -1
    is_retry: bool = False

# --- Profiling Decorator ---
def profile(fn):
    """Profiles function execution using cProfile + dumps stats."""
    def wrapper(*args, **kwargs):
        import cProfile
        profiler = cProfile.Profile()
        profiler.enable()
        try:
            return fn(*args, **kwargs)
        finally:
            profiler.disable()
            stats_path = f'/profiles/{fn.__name__}.prof'
            profiler.dump_stats(stats_path)
            logging.info(f'Profile data written to {stats_path}')
    return wrapper

# --- Leaky Bucket Throttler ---
class LeakyBucket:
    def __init__(self, capacity: int, leak_rate: float):
        self.capacity = capacity
        self.tokens = capacity
        self.leak_rate = leak_rate
        self.last_check = time.monotonic()

    def consume(self, amount: int = 1) -> bool:
        now = time.monotonic()
        elapsed = now - self.last_check
        self.tokens = min(self.capacity, self.tokens + elapsed * self.leak_rate)
        self.last_check = now
        TOKEN_BUCKET_GAUGE.set(self.tokens)
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False

# --- ChaosMonkey ---
class ChaosMonkey:
    def __init__(self):
        kube_config.load_incluster_config()
        self.core_v1 = kubernetes.client.CoreV1Api()

    def terminate_random_pod(self):
        pods = self.core_v1.list_namespaced_pod(
            namespace=CHAOS_NAMESPACE,
            label_selector=CHAOS_LABEL_SELECTOR
        ).items
        if not pods:
            return
        victim = random.choice(pods)
        self.core_v1.delete_namespaced_pod(
            name=victim.metadata.name,
            namespace=CHAOS_NAMESPACE
        )
        logging.warning(f"Chaos: Terminated pod {victim.metadata.name}")

# --- CachedSession with socket opts, retries, circuit breaker, caching ---
class CachedSession(requests.Session):
    def __init__(self):
        super().__init__()
        # Custom adapter for socket options + retries
        class SocketOptionsAdapter(HTTPAdapter):
            def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
                opts = [
                    (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
                    (socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                ]
                pool_kwargs['socket_options'] = opts
                super().init_poolmanager(connections, maxsize, block, **pool_kwargs)

        if ENABLE_RETRIES:
            retry_strategy = Retry(
                total=MAX_RETRIES,
                status_forcelist=RETRY_ON_STATUS_CODES,
                backoff_factor=RETRY_BACKOFF_FACTOR,
                allowed_methods=["HEAD","GET","OPTIONS","POST","PUT","DELETE"]
            )
            adapter = SocketOptionsAdapter(max_retries=retry_strategy)
        else:
            adapter = SocketOptionsAdapter()

        self.mount('http://', adapter)
        self.mount('https://', adapter)

    @CIRCUIT_BREAKER
    def request(self, method, url, **kwargs):
        if method.upper() == 'GET':
            cached = REDIS_CLIENT.get(url)
            if cached:
                resp = requests.Response._from_cache(cached)
                resp._cached = True
                return resp
        resp = super().request(method, url, **kwargs)
        if method.upper() == 'GET' and resp.status_code == 200:
            REDIS_CLIENT.setex(url, CACHE_TTL, resp._to_cache())
        return resp

# Thread-local session

def get_request_session() -> requests.Session:
    if not hasattr(thread_local, 'session'):
        session = CachedSession()
        session.headers.update(REQUEST_HEADERS)
        thread_local.session = session
    return thread_local.session

# --- Phase 1: Make Request ---
def make_request(request_id: int, is_retry: bool=False) -> RequestResult:
    start = time.monotonic()
    session = get_request_session()
    thread = threading.current_thread().name
    status_code = None
    latency_ms = None
    success = False
    error_type = None
    content_length = -1

    try:
        with LATENCY_HISTOGRAM.labels(TARGET_URL).time():
            resp = session.request(
                method=REQUEST_METHOD,
                url=TARGET_URL,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                allow_redirects=ALLOW_REDIRECTS,
                verify=VERIFY_SSL,
                stream=STREAM_RESPONSE
            )
        latency_ms = (time.monotonic() - start) * 1000
        status_code = resp.status_code
        REQUEST_COUNTER.labels(REQUEST_METHOD, status_code, TARGET_URL).inc()
        if 200 <= status_code < 300:
            success = True
        else:
            error_type = f"HTTP {status_code}"
            logging.warning(f"Req {request_id}: {error_type} in {latency_ms:.2f}ms")
        if STREAM_RESPONSE:
            cl = resp.headers.get('Content-Length')
            content_length = int(cl) if cl else -1
            for _ in resp.iter_content(8192): pass
            resp.close()
        else:
            content_length = len(resp.content)

    except pybreaker.CircuitBreakerError:
        ERROR_GAUGE.inc()
        error_type = 'CircuitOpen'
    except requests.exceptions.Timeout as e:
        latency_ms = (time.monotonic() - start) * 1000
        error_type = f"Timeout ({type(e).__name__})"
        logging.warning(f"Req {request_id}: {error_type}")
    except requests.exceptions.ConnectionError as e:
        latency_ms = (time.monotonic() - start) * 1000
        if isinstance(e.args[0], NewConnectionError):
            error_type = f"ConnError - {e.args[0]}"
        else:
            error_type = f"ConnectionError ({type(e).__name__})"
        logging.warning(f"Req {request_id}: {error_type}")
    except Exception as e:
        latency_ms = (time.monotonic() - start) * 1000
        error_type = f"Unexpected ({type(e).__name__})"
        logging.exception("Unexpected error")

    result = RequestResult(
        status_code=status_code,
        latency_ms=latency_ms,
        success=success,
        error_type=error_type,
        thread_name=thread,
        timestamp=time.time(),
        content_length=content_length,
        is_retry=is_retry
    )
    with results_lock:
        results_store.append(result)
        throttling_error_window.append(not success)
    return result

# --- Phase 2: Telemetry Analysis ---
def analyze_results(results: Deque[RequestResult], duration: float):
    logging.info("--- Phase 2: Telemetry Analysis ---")
    if not results:
        logging.warning("No results to analyze.")
        return
    total = len(results)
    logging.info(f"Completed {total} requests in {duration:.2f}s (avg RPS: {total/duration:.2f})")
    codes = collections.Counter(r.status_code for r in results if r.status_code is not None)
    errors = collections.Counter(r.error_type or 'Unknown' for r in results if not r.success)
    logging.info("Status codes distribution:")
    for code, cnt in codes.items():
        logging.info(f"  {code}: {cnt} ({cnt/total:.2%})")
    logging.info("Error types:")
    for et, cnt in errors.items():
        logging.info(f"  {et}: {cnt} ({cnt/total:.2%})")
    lat = sorted(r.latency_ms for r in results if r.latency_ms is not None)
    if lat:
        logging.info(f"Latency ms: min {min(lat):.2f}, avg {statistics.mean(lat):.2f}, max {max(lat):.2f}")
        for p in LATENCY_PERCENTILES:
            idx = min(int(len(lat)*(p/100)), len(lat)-1)
            logging.info(f"  {p}th percentile: {lat[idx]:.2f}")
    clens = [r.content_length for r in results if r.content_length>=0]
    if clens:
        logging.info(f"Content length bytes: min {min(clens)}, avg {statistics.mean(clens):.2f}, max {max(clens)}")
    logging.info("--- End Telemetry Analysis ---")

# --- Phase 3: Adaptive Throttling ---
def check_and_apply_throttling(level: int) -> Tuple[int, float]:
    if ENABLE_ADAPTIVE_THROTTLING and len(throttling_error_window) == THROTTLING_WINDOW_SIZE:
        err_rate = sum(throttling_error_window)/THROTTLING_WINDOW_SIZE
        if err_rate > ERROR_THRESHOLD:
            level += 1
            base = min(BACKOFF_WINDOW_MAX, BACKOFF_WINDOW_BASE * (2**level))
            sleep = min(BACKOFF_WINDOW_MAX, base + random.uniform(0, base*0.3))
            logging.warning(f"Throttling: err_rate {err_rate:.2%} > {ERROR_THRESHOLD:.1%}, sleeping {sleep:.2f}s")
            time.sleep(sleep)
            return level, sleep
        if level>0:
            logging.info(f"Err_rate {err_rate:.2%} <= threshold, resetting backoff")
            level = 0
    return level, 0.0

# --- Helpers ---
def log_config_details():
    logging.info("--- Configuration ---")
    logging.info(f"Target: {TARGET_URL}, Requests: {NUM_REQUESTS}, Workers: {NUM_WORKERS}")
    logging.info(f"Timeouts: {CONNECT_TIMEOUT}s connect, {READ_TIMEOUT}s read")
    logging.info(f"Retries: {ENABLE_RETRIES}, max {MAX_RETRIES}, backoff {RETRY_BACKOFF_FACTOR}")
    logging.info(f"Adaptive throttling: {ENABLE_ADAPTIVE_THROTTLING}, threshold {ERROR_THRESHOLD:.1%}")
    logging.info("----------------------")

def signal_handler(sig, frame):
    logging.warning(f"Signal {sig} received, shutting down...")
    shutdown_event.set()
    signal.signal(sig, signal.SIG_DFL)

# --- Chaos Loop ---
def _chaos_loop(monkey: ChaosMonkey):
    while not shutdown_event.is_set():
        time.sleep(random.uniform(CHAOS_INTERVAL_MIN, CHAOS_INTERVAL_MAX))
        monkey.terminate_random_pod()

# --- Main Orchestration ---
@profile
def run_stress_test():
    log_config_details()
    start_http_server(8000)
    chaos = ChaosMonkey()
    threading.Thread(target=_chaos_loop, args=(chaos,), daemon=True, name="ChaosMonkey").start()
    bucket = LeakyBucket(capacity=NUM_WORKERS*10, leak_rate=NUM_WORKERS/1.0)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=NUM_WORKERS)
    start = time.monotonic()
    submitted = 0
    backoff_level = 0

    while submitted < NUM_REQUESTS and not shutdown_event.is_set():
        if not bucket.consume():
            time.sleep(random.expovariate(1.0)); continue
        backoff_level, slept = check_and_apply_throttling(backoff_level)
        if slept>0: continue
        fut = executor.submit(make_request, submitted+1, False)
        with results_lock: active_futures.append(fut)
        submitted += 1
        now = time.monotonic()
        if (submitted % 100)==0:
            done = len(results_store)
            logging.info(f"Progress: {submitted}/{NUM_REQUESTS} submitted, {done} done, ~{done/(now-start):.1f} RPS")

    logging.info("All requests submitted, awaiting completion...")
    with results_lock: futures = list(active_futures)
    done, pending = concurrent.futures.wait(futures, timeout=CONNECT_TIMEOUT+READ_TIMEOUT+5)
    if pending:
        logging.warning(f"{len(pending)} tasks unfinished, cancelling...")
        for f in pending: f.cancel()
        executor.shutdown(wait=False)
    else:
        executor.shutdown(wait=True)
    duration = time.monotonic() - start
    analyze_results(results_store, duration)

if __name__ == '__main__':
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    if TARGET_URL.startswith('http://localhost'):
        logging.warning("Using default localhost URL—update TARGET_URL for real tests.")
    # Start py-spy for live profiling
    Popen(['py-spy', 'top', '--pid', str(os.getpid())])
    run_stress_test()
    logging.info("Script completed.")
