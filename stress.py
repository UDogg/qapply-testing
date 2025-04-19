#!/usr/bin/env python3

import requests
import concurrent.futures
import time
import random
import statistics
import logging
import collections
import socket
import threading
import signal
import sys
from typing import List, Tuple, Optional, Dict, Any, Deque, NamedTuple
from urllib3.exceptions import NewConnectionError
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --- Result Data Structure ---
class RequestResult(NamedTuple):
    """Holds the detailed outcome of a single HTTP request attempt."""
    status_code: Optional[int]
    latency_ms: Optional[float]
    success: bool
    error_type: Optional[str] # e.g., 'Timeout', 'ConnectionError', 'HTTP 503', 'SSL Error'
    thread_name: str
    timestamp: float # Time when the request finished
    content_length: Optional[int] = -1 # Optional: size of response body
    is_retry: bool = False # Was this result from a retry attempt?

# --- Configuration ---
# --- Core Settings ---
TARGET_URL = "http://localhost:8000"  # <<<*** REPLACE WITH YOUR TARGET URL ***>>>
# TARGET_URL = "https://httpbin.org/delay/1" # Example that introduces delay
NUM_REQUESTS = 5000   # Total synthetic requests to dispatch
NUM_WORKERS = 100     # Number of concurrent worker threads (adjust based on client resources)

# --- Request Behavior ---
REQUEST_METHOD = "GET" # Could be GET, POST, PUT, etc. (POST/PUT would need data)
REQUEST_HEADERS = {    # Custom headers if needed
    'User-Agent': 'EnhancedPythonStressTester/1.0',
    'Accept': '*/*',
    # 'Authorization': 'Bearer YOUR_TOKEN' # Example
}
CONNECT_TIMEOUT = 5.0  # Seconds to wait for connection establishment
READ_TIMEOUT = 15.0    # Seconds to wait for the server to send data after connection
ALLOW_REDIRECTS = True # Follow HTTP redirects (3xx)
VERIFY_SSL = True      # Verify SSL certificates. WARNING: False is insecure, only for trusted test envs.
STREAM_RESPONSE = False # If True, doesn't download body immediately. Useful for large files or just checking headers.

# --- Retry Logic ---
ENABLE_RETRIES = True # Enable automatic retries for specific conditions
MAX_RETRIES = 3       # Maximum number of retries per request
RETRY_BACKOFF_FACTOR = 0.5 # Backoff delay = {backoff factor} * (2 ** ({number of total retries} - 1))
RETRY_ON_STATUS_CODES = {500, 502, 503, 504} # HTTP status codes to retry on
RETRY_ON_CONNECTION_ERRORS = True # Retry on connection errors/timeouts?

# --- Adaptive Throttling ---
ENABLE_ADAPTIVE_THROTTLING = True
ERROR_THRESHOLD = 0.05 # Trigger backoff if error rate exceeds 5% over the window
THROTTLING_WINDOW_SIZE = 200 # Calculate error rate based on the last N results
BACKOFF_WINDOW_BASE = 0.1 # Starting backoff delay in seconds
BACKOFF_WINDOW_MAX = 10.0 # Maximum seconds for exponential backoff window

# --- Analysis & Reporting ---
LATENCY_PERCENTILES = [50, 75, 90, 95, 99, 99.9] # Percentiles to calculate
LOG_LEVEL = logging.INFO # DEBUG, INFO, WARNING, ERROR

# --- Global State & Control ---
# Using Deque for efficient append/popleft, suitable for sliding window
results_store: Deque[RequestResult] = collections.deque()
# This deque specifically for the *adaptive throttling* error rate calculation
throttling_error_window: Deque[bool] = collections.deque(maxlen=THROTTLING_WINDOW_SIZE)
# Thread-local storage for session objects to ensure thread safety and connection pooling per thread
thread_local_data = threading.local()
# Graceful shutdown flag
shutdown_event = threading.Event()
# Keep track of submitted futures for graceful shutdown
active_futures: List[concurrent.futures.Future] = []
# Lock for safely appending to shared results_store and managing active_futures list
results_lock = threading.Lock()

# --- Logging Setup ---
logging.basicConfig(
    level=LOG_LEVEL,
    format='%(asctime)s - %(levelname)s - [%(threadName)s] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
# Suppress overly verbose logs from underlying libraries if desired
# logging.getLogger("urllib3").setLevel(logging.WARNING)
# logging.getLogger("requests").setLevel(logging.WARNING)

# --- Utility Functions ---
def get_request_session() -> requests.Session:
    """
    Creates or retrieves a requests.Session object for the current thread.
    Configures session-level settings like retries and potential custom adapters.
    """
    if not hasattr(thread_local_data, 'session'):
        logging.debug("Creating new requests.Session for thread %s", threading.current_thread().name)
        session = requests.Session()
        session.headers.update(REQUEST_HEADERS)

        adapter_args = {}

        # --- Optimization: Configure Retries (if enabled) ---
        # Leverage built-in urllib3 retries within requests Session
        if ENABLE_RETRIES:
            retry_strategy = Retry(
                total=MAX_RETRIES,
                status_forcelist=RETRY_ON_STATUS_CODES,
                backoff_factor=RETRY_BACKOFF_FACTOR,
                method_whitelist=["HEAD", "GET", "OPTIONS", "POST", "PUT", "DELETE"], # Methods to retry on
                # Note: Retry on connection errors is implicitly handled by `total` unless specifically disabled.
                # We rely on catching exceptions later for connection error categorization if needed.
            )
            # Mount it for both http and https prefixes
            adapter_args['max_retries'] = retry_strategy

        # --- Optimization: Custom Adapter for Socket Options (Advanced Example) ---
        # Uncomment and customize if needed. This adds complexity.
        # class SocketOptionsAdapter(HTTPAdapter):
        #     def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        #         socket_options = [(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)] # Example: Enable TCP_NODELAY
        #         # Add SO_REUSEADDR if needed, though more relevant server-side usually
        #         # socket_options.append((socket.SOL_SOCKET, socket.SO_REUSEADDR, 1))
        #         pool_kwargs['socket_options'] = socket_options
        #         super().init_poolmanager(connections, maxsize, block, **pool_kwargs)
        # adapter = SocketOptionsAdapter(**adapter_args)

        # --- Standard Adapter with Retries ---
        adapter = HTTPAdapter(**adapter_args)
        session.mount('http://', adapter)
        session.mount('https://', adapter)

        thread_local_data.session = session
    return thread_local_data.session

# --- Phase 1: Flood Generation & Request Execution ---
def make_request(url: str, request_id: int, is_retry_attempt: bool = False) -> RequestResult:
    """
    Sends a single HTTP request using the thread-local session, measures latency,
    handles various errors, and returns a detailed result object.

    Args:
        url: The target URL.
        request_id: A unique identifier for logging/tracking this specific request attempt.
        is_retry_attempt: Flag indicating if this call is a retry.

    Returns:
        RequestResult containing the outcome.
    """
    start_time = time.monotonic()
    session = get_request_session()
    thread_name = threading.current_thread().name
    status_code: Optional[int] = None
    latency_ms: Optional[float] = None
    success = False
    error_type: Optional[str] = None
    content_length: Optional[int] = -1
    response: Optional[requests.Response] = None

    try:
        logging.debug(f"Req {request_id}: Attempting {REQUEST_METHOD} to {url}")
        response = session.request(
            method=REQUEST_METHOD,
            url=url,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            allow_redirects=ALLOW_REDIRECTS,
            verify=VERIFY_SSL,
            stream=STREAM_RESPONSE, # Process response differently if streaming
            # data=payload if REQUEST_METHOD in ['POST', 'PUT'] else None # Add payload if needed
        )
        latency_ms = (time.monotonic() - start_time) * 1000
        status_code = response.status_code

        # Determine success based on status code
        if 200 <= status_code < 300:
            success = True
            logging.debug(f"Req {request_id}: Success ({status_code}), Latency: {latency_ms:.2f} ms")
        else:
            success = False
            error_type = f"HTTP {status_code}"
            logging.warning(f"Req {request_id}: Failure ({error_type}), Latency: {latency_ms:.2f} ms")

        # Handle streamed response if enabled
        if STREAM_RESPONSE:
            try:
                # Try to get content length without downloading body
                cl = response.headers.get('Content-Length')
                content_length = int(cl) if cl is not None else -1
                # Consume the body to free the connection, even if streaming
                # response.raise_for_status() # Optional: raise exception for bad status codes immediately
                for _ in response.iter_content(chunk_size=8192):
                    pass # Just consume
            except Exception as stream_exc:
                 # Handle potential errors during stream consumption
                 success = False # Mark as failure if stream consumption fails
                 error_type = f"Stream Error ({type(stream_exc).__name__})"
                 logging.error(f"Req {request_id}: Error consuming streamed response: {stream_exc}")
            finally:
                response.close() # Ensure connection is released
        else:
             # Get content length for non-streamed responses
            try:
                content_length = len(response.content)
            except Exception:
                 content_length = -1 # Handle cases where content is not available/error

        # Explicitly raise status for non-2xx codes if not automatically retried
        # Note: The Retry logic in the adapter might handle some cases automatically.
        # If status is bad here, it means retries (if enabled) were exhausted or status not configured for retry.
        # response.raise_for_status() # We log it as an error instead of raising here


    # --- Granular Error Handling ---
    except requests.exceptions.Timeout as e:
        # Catches both ConnectTimeout and ReadTimeout
        latency_ms = (time.monotonic() - start_time) * 1000
        success = False
        error_type = f"Timeout ({type(e).__name__})"
        logging.warning(f"Req {request_id}: {error_type}, Latency: {latency_ms:.2f} ms")
    except requests.exceptions.ConnectionError as e:
        # Covers DNS errors, refused connections, etc.
        # NewConnectionError is a subclass often seen
        latency_ms = (time.monotonic() - start_time) * 1000
        success = False
        error_type = f"ConnectionError ({type(e).__name__})"
        # Log specific underlying error if available (e.g., from urllib3)
        if isinstance(e.args[0], NewConnectionError):
             error_type += f" - {e.args[0]}" # More specific info
        logging.warning(f"Req {request_id}: {error_type}, Latency: {latency_ms:.2f} ms")
    except requests.exceptions.SSLError as e:
        latency_ms = (time.monotonic() - start_time) * 1000
        success = False
        error_type = f"SSLError"
        logging.error(f"Req {request_id}: SSL Verification failed: {e}")
    except requests.exceptions.TooManyRedirects as e:
        latency_ms = (time.monotonic() - start_time) * 1000
        success = False
        error_type = "TooManyRedirects"
        logging.warning(f"Req {request_id}: Exceeded max redirects. Check URL or {ALLOW_REDIRECTS=}. Error: {e}")
    except requests.exceptions.RequestException as e:
        # Catch-all for other requests-related errors (InvalidURL, ChunkedEncodingError, etc.)
        latency_ms = (time.monotonic() - start_time) * 1000
        success = False
        error_type = f"RequestException ({type(e).__name__})"
        logging.error(f"Req {request_id}: General request error: {e}", exc_info=False) # exc_info=True for full traceback
    except Exception as e:
        # Catch unexpected errors within the function
        latency_ms = (time.monotonic() - start_time) * 1000
        success = False
        error_type = f"UnexpectedError ({type(e).__name__})"
        logging.exception(f"Req {request_id}: An unexpected error occurred: {e}") # Log full traceback for unexpected
    finally:
        # Ensure response is closed if it exists and streaming wasn't used (or failed)
        if response and not STREAM_RESPONSE:
            response.close()

    # --- Record Result ---
    result = RequestResult(
        status_code=status_code,
        latency_ms=latency_ms,
        success=success,
        error_type=error_type,
        thread_name=thread_name,
        timestamp=time.time(), # Use wall clock time for timestamping results
        content_length=content_length,
        is_retry=is_retry_attempt
    )

    # Safely append to shared data structures
    with results_lock:
        results_store.append(result)
        # Update the throttling window (only tracks success/failure)
        throttling_error_window.append(not success) # Append True if error, False if success

    return result


# --- Phase 2: Telemetry Pipeline (Enhanced Analysis) ---
def analyze_results(results: Deque[RequestResult], duration_sec: float):
    """
    Calculates and logs detailed statistics from the collected results.

    Args:
        results: Deque containing RequestResult objects.
        duration_sec: The total duration of the test execution phase.
    """
    logging.info("--- Phase 2: Telemetry Analysis ---")

    if not results:
        logging.warning("No results collected to analyze.")
        return

    total_completed = len(results)
    logging.info(f"Analysis based on {total_completed} completed requests over {duration_sec:.2f} seconds.")

    # Calculate overall Requests Per Second (RPS)
    if duration_sec > 0:
        overall_rps = total_completed / duration_sec
        logging.info(f"Overall Average RPS: {overall_rps:.2f}")
    else:
        logging.info("Overall Average RPS: N/A (duration too short)")


    status_codes = collections.defaultdict(int)
    error_types = collections.defaultdict(int)
    latencies_ms: List[float] = []
    content_lengths: List[int] = []
    success_count = 0
    retry_attempts_recorded = 0 # Note: This counts results marked as retries, not total attempts

    for res in results:
        if res.latency_ms is not None:
            latencies_ms.append(res.latency_ms)
        if res.status_code is not None:
            status_codes[res.status_code] += 1
        if res.success:
            success_count += 1
        else:
            error_key = res.error_type if res.error_type else "Unknown Error"
            error_types[error_key] += 1
        if res.content_length is not None and res.content_length >= 0:
             content_lengths.append(res.content_length)
        if res.is_retry:
            retry_attempts_recorded += 1 # Count results that were the product of a retry


    failure_count = total_completed - success_count
    logging.info(f"Successful Requests: {success_count}")
    logging.info(f"Failed Requests: {failure_count}")
    if total_completed > 0:
        logging.info(f"Success Rate: {success_count / total_completed:.2%}")
    # if ENABLE_RETRIES:
        # logging.info(f"Retry Results Recorded: {retry_attempts_recorded} (Note: Represents successful retries or final failed retries)")


    logging.info("\n--- Status Code Distribution ---")
    if status_codes:
        for code, count in sorted(status_codes.items()):
            logging.info(f"  HTTP {code}: {count} ({count / total_completed:.2%})")
    else:
        logging.info("  No status codes recorded.")

    logging.info("\n--- Error Types Breakdown ---")
    if error_types:
        for err_type, count in sorted(error_types.items(), key=lambda item: item[1], reverse=True):
            logging.info(f"  {err_type}: {count} ({count / total_completed:.2%})")
    else:
        logging.info("  No errors recorded.")


    logging.info("\n--- Latency Statistics (ms) ---")
    if latencies_ms:
        latencies_ms.sort() # Sort for percentile calculation
        min_lat = latencies_ms[0]
        max_lat = latencies_ms[-1]
        avg_lat = statistics.mean(latencies_ms)
        median_lat = statistics.median(latencies_ms)
        stdev_lat = statistics.stdev(latencies_ms) if len(latencies_ms) > 1 else 0.0

        logging.info(f"  Count: {len(latencies_ms)}")
        logging.info(f"  Min: {min_lat:.2f}")
        logging.info(f"  Average: {avg_lat:.2f}")
        logging.info(f"  Median: {median_lat:.2f}")
        logging.info(f"  Max: {max_lat:.2f}")
        logging.info(f"  Std Dev: {stdev_lat:.2f}")

        logging.info("  Percentiles:")
        # Calculate requested percentiles efficiently on sorted list
        for p in LATENCY_PERCENTILES:
            try:
                # Simple percentile calculation (index = p/100 * N)
                idx = int(len(latencies_ms) * (p / 100.0))
                # Clamp index to valid range [0, N-1]
                idx = max(0, min(idx, len(latencies_ms) - 1))
                p_val = latencies_ms[idx]
                logging.info(f"  {p}th: {p_val:.2f}")
            except IndexError:
                logging.warning(f"Could not calculate {p}th percentile (not enough data).")
            except Exception as e:
                 logging.error(f"Error calculating {p}th percentile: {e}")

    else:
        logging.info("  No latency data recorded.")


    logging.info("\n--- Content Length Statistics (bytes) ---")
    if content_lengths:
        min_len = min(content_lengths)
        max_len = max(content_lengths)
        avg_len = statistics.mean(content_lengths)
        median_len = statistics.median(content_lengths)
        stdev_len = statistics.stdev(content_lengths) if len(content_lengths) > 1 else 0.0

        logging.info(f"  Count: {len(content_lengths)}")
        logging.info(f"  Min: {min_len}")
        logging.info(f"  Average: {avg_len:.2f}")
        logging.info(f"  Median: {median_len:.0f}")
        logging.info(f"  Max: {max_len}")
        logging.info(f"  Std Dev: {stdev_len:.2f}")
    else:
        logging.info("  No content length data recorded.")


    logging.info("--- End Telemetry Analysis ---")

# --- Phase 3: Adaptive Throttling ---
def check_and_apply_throttling(current_error_rate: float, backoff_level: int) -> Tuple[int, float]:
    """
    Checks if the error rate exceeds the threshold and applies exponential backoff.

    Args:
        current_error_rate: The calculated error rate over the window.
        backoff_level: The current level/attempt number for backoff calculation.

    Returns:
        Tuple containing the updated backoff_level and the calculated sleep_duration.
    """
    sleep_duration = 0.0
    if ENABLE_ADAPTIVE_THROTTLING and current_error_rate > ERROR_THRESHOLD:
        backoff_level += 1
        # Exponential backoff with jitter
        base_delay = min(BACKOFF_WINDOW_MAX, BACKOFF_WINDOW_BASE * (2 ** backoff_level))
        jitter = random.uniform(0, base_delay * 0.3) # Add jitter (e.g., up to 30%)
        sleep_duration = min(BACKOFF_WINDOW_MAX, base_delay + jitter) # Ensure max isn't exceeded

        logging.warning(
            f"Throttling: Error rate ({current_error_rate:.2%}) > threshold ({ERROR_THRESHOLD:.1%}). "
            f"Backing off for {sleep_duration:.3f} seconds (Level {backoff_level})."
        )
        time.sleep(sleep_duration)
    elif backoff_level > 0:
        # Reset backoff level if error rate is acceptable again
        logging.info(f"Throttling: Error rate ({current_error_rate:.2%}) <= threshold ({ERROR_THRESHOLD:.1%}). Resuming normal rate.")
        backoff_level = 0

    return backoff_level, sleep_duration

# --- Main Orchestration & Graceful Shutdown ---
def run_stress_test(url: str, total_requests_target: int, num_workers: int):
    """
    Orchestrates the concurrent request generation, manages workers,
    applies throttling, and handles graceful shutdown.
    """
    log_config_details()
    logging.info(f"--- Starting Stress Test ---")

    # Using ThreadPoolExecutor for I/O-bound concurrency
    # Worker threads will be created up to num_workers as needed
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=num_workers, thread_name_prefix='StressWorker')

    requests_submitted = 0
    backoff_level = 0
    last_progress_log_time = time.monotonic()
    last_progress_req_count = 0

    global active_futures
    start_time = time.monotonic()

    try:
        while requests_submitted < total_requests_target:
            if shutdown_event.is_set():
                logging.warning("Shutdown signal received. Stopping submission of new requests.")
                break

            # --- Adaptive Throttling Check ---
            current_error_rate = 0.0
            with results_lock: # Access throttling window safely
                if len(throttling_error_window) == THROTTLING_WINDOW_SIZE:
                    recent_errors = sum(1 for is_error in throttling_error_window if is_error)
                    current_error_rate = recent_errors / THROTTLING_WINDOW_SIZE

            backoff_level, sleep_duration = check_and_apply_throttling(current_error_rate, backoff_level)

            # If we slept, continue loop to re-check shutdown flag and error rate
            if sleep_duration > 0:
                continue

            # --- Submit Task ---
            # Ensure we don't submit more tasks than the pool can handle queueing
            # Check queue size roughly (not perfectly accurate)
            # This prevents memory exhaustion if tasks back up faster than workers process
            # Note: _work_queue is internal, use with caution or implement custom queueing
            if hasattr(executor, '_work_queue') and executor._work_queue.qsize() >= num_workers * 2:
                time.sleep(0.05) # Short pause if queue is filling up
                continue

            future = executor.submit(make_request, url, requests_submitted + 1)
            with results_lock:
                 active_futures.append(future) # Track for shutdown
            requests_submitted += 1

            # --- Progress Logging ---
            now = time.monotonic()
            if now - last_progress_log_time >= 5.0: # Log progress every 5 seconds
                current_total_completed = len(results_store)
                requests_in_period = current_total_completed - last_progress_req_count
                time_elapsed_period = now - last_progress_log_time
                current_rps = requests_in_period / time_elapsed_period if time_elapsed_period > 0 else 0

                logging.info(
                    f"Progress: {requests_submitted}/{total_requests_target} submitted. "
                    f"{current_total_completed} completed. "
                    f"~{current_rps:.1f} RPS (last 5s). "
                    f"Error rate (window): {current_error_rate:.2%}. "
                    f"Active Workers: {len(executor._threads)}. "
                    f"Queue Size: {executor._work_queue.qsize() if hasattr(executor, '_work_queue') else 'N/A'}."
                )
                last_progress_log_time = now
                last_progress_req_count = current_total_completed

        logging.info(f"--- All {requests_submitted}/{total_requests_target} requests submitted. Waiting for completion... ---")

    except KeyboardInterrupt:
        logging.warning("KeyboardInterrupt received during submission. Initiating graceful shutdown.")
        shutdown_event.set()
    except Exception as e:
        logging.exception("An unexpected error occurred during test orchestration.")
        shutdown_event.set()
    finally:
        # --- Graceful Shutdown ---
        logging.info("Shutting down thread pool executor...")
        # We don't submit new tasks now (due to loop break or exception)

        # Wait for already submitted tasks to complete, up to a timeout
        # Make a copy of the futures list to avoid issues if modified elsewhere
        with results_lock:
             futures_to_wait_for = list(active_futures)

        # Using 'wait' allows for timeout and returns completed/not_completed sets
        # (though we don't explicitly use the sets here, just wait)
        # Set a reasonable timeout for pending tasks to finish
        shutdown_wait_timeout = READ_TIMEOUT + CONNECT_TIMEOUT + 5.0 # Generous timeout
        logging.info(f"Waiting up to {shutdown_wait_timeout:.1f} seconds for {len(futures_to_wait_for)} active tasks to complete...")
        # executor.shutdown(wait=True) # This waits indefinitely, potentially blocking shutdown
        # Use concurrent.futures.wait for timeout control
        done, not_done = concurrent.futures.wait(futures_to_wait_for, timeout=shutdown_wait_timeout)

        if not_done:
            logging.warning(f"{len(not_done)} tasks did not complete within the shutdown timeout. Forcing shutdown.")
            # Force shutdown - attempt to cancel remaining futures (may not work if running)
            for future in not_done:
                 future.cancel()
            executor.shutdown(wait=False) 
            # Don't wait further
        else:
            logging.info("All active tasks completed.")
            executor.shutdown(wait=True) 
            # Standard cleanup

        logging.info("--- Stress Test Execution Phase Finished ---")
        end_time = time.monotonic()
        duration = end_time - start_time
        # Analyze whatever results were collected, even if interrupted
        analyze_results(results_store, duration)


def signal_handler(sig, frame):
    """Sets the shutdown event when SIGINT or SIGTERM is received."""
    logging.warning(f"Received signal {sig}. Initiating graceful shutdown...")
    shutdown_event.set()
    # Wxit after a second signal if shutdown is stuck
    signal.signal(sig, signal.SIG_DFL) 
    # Register default handler for second signal

def log_config_details():
    """Logs the configuration settings being used for the test run."""
    logging.info("--- Configuration Settings ---")
    logging.info(f"Target URL: {TARGET_URL}")
    logging.info(f"Request Method: {REQUEST_METHOD}")
    logging.info(f"Total Requests: {NUM_REQUESTS}")
    logging.info(f"Concurrent Workers: {NUM_WORKERS}")
    logging.info(f"Timeouts (Connect/Read): {CONNECT_TIMEOUT:.1f}s / {READ_TIMEOUT:.1f}s")
    logging.info(f"Allow Redirects: {ALLOW_REDIRECTS}")
    logging.info(f"Verify SSL: {VERIFY_SSL}")
    logging.info(f"Stream Response Body: {STREAM_RESPONSE}")
    logging.info(f"Custom Headers: {'Yes' if REQUEST_HEADERS else 'No'}")
    logging.info(f"Retries Enabled: {ENABLE_RETRIES} (Max: {MAX_RETRIES}, Backoff: {RETRY_BACKOFF_FACTOR}, Statuses: {RETRY_ON_STATUS_CODES})")
    logging.info(f"Adaptive Throttling Enabled: {ENABLE_ADAPTIVE_THROTTLING} (Threshold: {ERROR_THRESHOLD:.1%}, Window: {THROTTLING_WINDOW_SIZE})")
    logging.info(f"Latency Percentiles: {LATENCY_PERCENTILES}")
    logging.info(f"Log Level: {logging.getLevelName(LOG_LEVEL)}")
    logging.info("-----------------------------")

# --- Phase 4 & 5 Commentary ---
# Phase 4 (Bottleneck Analysis) & Phase 5 (Chaos Drills) remain external processes.
# This script provides the load; analysis (Py-Spy, cProfile, DB tools) and chaos
# injection (kube-monkey, network tools) happen concurrently or against the
# environment being tested by this script. The improved telemetry output
# (error types, detailed latencies) from this script aids in correlating
# observations during those external activities.


# --- Main Execution ---
if __name__ == "__main__":
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler) # Ctrl+C
    signal.signal(signal.SIGTERM, signal_handler) # kill command

    if TARGET_URL == "http://localhost:8000" or TARGET_URL == "http://example.com":
      # For privacy reasons, I changed this
        logging.warning("=" * 60)
        logging.warning("WARNING: Target URL is set to a default/example.")
        logging.warning("Please change the `TARGET_URL` variable in the script!")
        logging.warning("Ensure you have permission to test the target system.")
        logging.warning("=" * 60)

    # Run the main test function
    run_stress_test(TARGET_URL, NUM_REQUESTS, NUM_WORKERS)

    logging.info("--- Script Finished ---")
