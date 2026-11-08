#!/usr/bin/env python3
"""
scripts/probe_models.py

Probes a series of Gemini flash model variants against the configured LLM_BASE_URL
to determine availability and latency without leaking credentials.

Usage:
    python scripts/probe_models.py
"""

import os
import sys
import time
from pathlib import Path
import httpx
from dotenv import load_dotenv

# Load .env from repo root
REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env")

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "").rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")

CANDIDATE_MODELS = [
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
]


def probe_model(model: str, endpoint: str, headers: dict) -> None:
    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": "Reply with ok"}
        ],
        "max_tokens": 50,
    }

    start = time.perf_counter()
    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(endpoint, headers=headers, json=payload)
        elapsed = time.perf_counter() - start
        print(f"{model}: HTTP {resp.status_code} in {elapsed:.2f}s")
    except Exception as exc:
        elapsed = time.perf_counter() - start
        exc_type = type(exc).__name__
        print(f"{model}: {exc_type} after {elapsed:.2f}s")


def main():
    if not LLM_BASE_URL:
        print("[FATAL] Missing LLM_BASE_URL")
        sys.exit(1)
    if not LLM_API_KEY:
        print("[FATAL] Missing LLM_API_KEY")
        sys.exit(1)

    endpoint = f"{LLM_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }

    print("=" * 60)
    print("PROBING CANDIDATE MODELS")
    print(f"Base URL: {LLM_BASE_URL}")
    print("=" * 60)

    for model in CANDIDATE_MODELS:
        probe_model(model, endpoint, headers)


if __name__ == "__main__":
    main()

