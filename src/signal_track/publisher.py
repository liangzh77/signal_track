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
    address = data.get("address")
    return str(address) if address else None
