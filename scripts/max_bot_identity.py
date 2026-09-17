#!/usr/bin/env python3
"""Validate a MAX bot token and print only its public identity."""

from __future__ import annotations

import os
import sys

import truststore

truststore.inject_into_ssl()

import httpx


def fetch_identity(token: str, api_url: str) -> tuple[str, str]:
    response = httpx.get(
        f"{api_url.rstrip('/')}/me",
        headers={"Authorization": token},
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    user_id = payload.get("user_id")
    username = payload.get("username")
    if user_id is None or not isinstance(username, str) or not username.strip():
        raise ValueError("MAX /me returned an incomplete bot identity")
    return str(user_id), username.strip()


def main() -> int:
    allow_empty = "--allow-empty" in sys.argv[1:]
    token = os.getenv("TOKEN", "").strip()
    if not token:
        if allow_empty:
            return 3
        print("MAX bot token is empty", file=sys.stderr)
        return 1

    try:
        user_id, username = fetch_identity(
            token,
            os.getenv("MAX_API_URL", "https://platform-api2.max.ru"),
        )
    except httpx.HTTPStatusError as exc:
        print(
            f"MAX /me rejected the bot token (HTTP {exc.response.status_code})",
            file=sys.stderr,
        )
        return 1
    except (httpx.HTTPError, ValueError) as exc:
        print(f"MAX /me validation failed: {type(exc).__name__}", file=sys.stderr)
        return 1

    print(f"{user_id}\t{username}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
