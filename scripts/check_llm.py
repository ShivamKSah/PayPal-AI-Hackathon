#!/usr/bin/env python3
"""
scripts/check_llm.py

Verifies LLM provider connectivity using OpenAI-compatible chat completions endpoint.
Reads LLM_BASE_URL, LLM_API_KEY, and LLM_MODEL from environment / .env.

Usage:
    python scripts/check_llm.py
"""

import os
import sys
from pathlib import Path
import httpx
from dotenv import load_dotenv

# Load .env from repo root
REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env")

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "").rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "")


def main():
    print("=" * 70)
    print("MANDATE LLM CONNECTIVITY CHECK")
    print("=" * 70)

    # 1. Environment validation
    missing = []
    if not LLM_BASE_URL:
        missing.append("LLM_BASE_URL")
    if not LLM_API_KEY:
        missing.append("LLM_API_KEY")
    if not LLM_MODEL:
        missing.append("LLM_MODEL")

    if missing:
        print(f"[FATAL] Missing required environment variable(s): {', '.join(missing)}")
        print("Please configure them in your .env file or environment.")
        sys.exit(1)

    print(f"Provider URL:    {LLM_BASE_URL}")
    print(f"Model Requested: {LLM_MODEL}")
    print("Sending chat completion test request...")

    endpoint = f"{LLM_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "user", "content": "Reply with the single word ok"}
        ],
        "max_tokens": 20,
    }

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(endpoint, headers=headers, json=payload)

        status_code = resp.status_code
        print(f"HTTP Status:     {status_code}")

        if resp.is_success:
            data = resp.json()
            reported_model = data.get("model", "unknown")
            reply_text = ""
            choices = data.get("choices", [])
            if choices and isinstance(choices, list):
                first_choice = choices[0]
                if "message" in first_choice and "content" in first_choice["message"]:
                    reply_text = first_choice["message"]["content"].strip()

            print(f"Model Reported:  {reported_model}")
            print(f"Reply Text:      {reply_text}")
            print("-" * 70)
            print("RESULT: SUCCESS")
            print("=" * 70)
            sys.exit(0)
        else:
            error_snippet = resp.text[:300]
            print("-" * 70)
            print(f"Error Details (first 300 chars): {error_snippet}")
            print("-" * 70)
            print("RESULT: FAILED")
            print("=" * 70)
            sys.exit(1)

    except Exception as exc:
        print("-" * 70)
        print(f"Request Exception: {str(exc)[:300]}")
        print("-" * 70)
        print("RESULT: FAILED")
        print("=" * 70)
        sys.exit(1)


if __name__ == "__main__":
    main()

