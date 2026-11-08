"""LLM client with automatic retry and fast-model fallback.

Implements resilient OpenAI-compatible chat completions:
1. Retries the primary model up to 2 times on transient failures (429, 500, 502, 503, 504, timeout).
2. Falls back to LLM_MODEL_FAST if primary model attempts are exhausted and fast model differs.
3. If all attempts fail, raises for caller to log WARNING and fall back to deterministic rules.
"""

import time
import logging
from typing import Dict, Any, Tuple, Callable
import httpx

from backend.app import config

logger = logging.getLogger("mandate.llm")

TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}


class LLMRetryError(RuntimeError):
    def __init__(self, message: str, attempts: int, last_status: Any, last_error: str):
        super().__init__(message)
        self.attempts = attempts
        self.last_status = last_status
        self.last_error = last_error


def call_llm_with_retry(
    payload_builder: Callable[[str], Dict[str, Any]],
    timeout: float = 45.0,
    backoff_delays: Tuple[float, float] = (1.0, 3.0),
) -> Tuple[Dict[str, Any], str, int]:
    """
    Execute chat completion with automatic retry and fast-model fallback.
    - Retry primary model up to 2 times on 429, 500, 502, 503, 504, or timeout.
    - If still failing, retry once with LLM_MODEL_FAST if distinct from LLM_MODEL.
    - If all fail, raises LLMRetryError for caller to log warning and fall back to rules.
    Returns (response_json, model_used, total_attempts).
    """
    llm_base_url = getattr(config, "LLM_BASE_URL", "https://openrouter.ai/api/v1")
    llm_api_key = getattr(config, "LLM_API_KEY", "")
    llm_model = getattr(config, "LLM_MODEL", "google/gemini-2.5-flash")
    llm_model_fast = getattr(config, "LLM_MODEL_FAST", "")

    url = f"{llm_base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {llm_api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/mandate",
        "X-Title": "Mandate Policy Engine",
    }

    attempts = 0
    last_error = ""
    last_status = None

    # Step 1: Try primary model up to 3 times (initial + 2 retries on 500, 502, 503, 504, timeout).
    # On HTTP 429 (quota exceeded), do NOT retry primary; go straight to LLM_MODEL_FAST.
    for try_idx in range(3):
        attempts += 1
        payload = payload_builder(llm_model)

        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(url, headers=headers, json=payload)

            if resp.status_code == 200:
                data = resp.json()
                reported_model = data.get("model") or llm_model
                return data, reported_model, attempts

            last_status = resp.status_code
            reason = getattr(resp, "reason_phrase", "") or ""
            last_error = f"HTTP {resp.status_code} ({reason}): {resp.text[:300]}"

            # On 429 (quota exceeded), do NOT retry the same model; break immediately to fast model
            if resp.status_code == 429:
                break

            # On non-retryable 4xx errors, break to fast model
            if resp.status_code not in TRANSIENT_STATUS_CODES:
                break

        except (httpx.TimeoutException, httpx.NetworkError, Exception) as exc:
            last_error = str(exc)
            last_status = "TIMEOUT/EXCEPTION"

        # Apply short backoff between retries of the primary model (1s, then 3s)
        if try_idx == 0 and len(backoff_delays) > 0 and backoff_delays[0] > 0:
            time.sleep(backoff_delays[0])
        elif try_idx == 1 and len(backoff_delays) > 1 and backoff_delays[1] > 0:
            time.sleep(backoff_delays[1])

    # Step 2: If primary model failed and fast model is configured and different, try fast model once
    if llm_model_fast and llm_model_fast != llm_model:
        attempts += 1
        payload = payload_builder(llm_model_fast)
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(url, headers=headers, json=payload)

            if resp.status_code == 200:
                data = resp.json()
                reported_model = data.get("model") or llm_model_fast
                return data, reported_model, attempts

            last_status = resp.status_code
            reason = getattr(resp, "reason_phrase", "") or ""
            last_error = f"HTTP {resp.status_code} ({reason}): {resp.text[:300]}"
        except (httpx.TimeoutException, httpx.NetworkError, Exception) as exc:
            last_error = str(exc)
            last_status = "TIMEOUT/EXCEPTION"

    raise LLMRetryError(
        f"All LLM attempts failed ({attempts} attempts, last status {last_status}): {last_error}",
        attempts=attempts,
        last_status=last_status,
        last_error=last_error,
    )
