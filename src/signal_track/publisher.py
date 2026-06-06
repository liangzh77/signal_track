from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class PublishResult:
    ok: bool
    status_code: int | None
    body: str


class DemoPublisher:
    def __init__(self, publish_url: str, api_key: str):
        self.publish_url = publish_url
        self.api_key = api_key

    def publish(
        self,
        title: str,
        html: str,
        feature: str = "Signal Track 自动发布",
        disabled: bool = False,
    ) -> PublishResult:
        payload = json.dumps(
            {
                "title": title,
                "html": html,
                "feature": feature,
                "disabled": disabled,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.publish_url,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8", errors="replace")
                return PublishResult(True, response.status, body)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return PublishResult(False, exc.code, body)
        except urllib.error.URLError as exc:
            return PublishResult(False, None, str(exc.reason))


def extract_published_address(body: str) -> str | None:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    return find_address(data)


def find_address(value) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in ("address", "url", "public_url", "href"):
        address = value.get(key)
        if address:
            return str(address)
    for key in ("demo", "item", "data"):
        nested = find_address(value.get(key))
        if nested:
            return nested
    return None


def publish_payload(result: PublishResult, publish_url: str | None = None, body_limit: int = 500) -> dict:
    body_preview = result.body[:body_limit] if result.body else ""
    return {
        "attempted": True,
        "ok": result.ok,
        "status_code": result.status_code,
        "url": extract_published_address(result.body),
        "publish_url": publish_url,
        "error": None if result.ok else body_preview,
        "response_body": body_preview,
    }
