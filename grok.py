import json
import os

import httpx
import requests

from browser_upstream import BrowserGrokRequest
from changecookie import ChangeCookie


class HttpGrokRequest:
    grok_url: str = "https://grok.com/rest/app-chat/conversations/new"

    base_headers = {
        "authority": "grok.com",
        "accept": "*/*",
        "content-type": "application/json",
        "origin": "https://grok.com",
        "referer": "https://grok.com/?referrer=website",
    }

    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(240),
            http2=True,
            follow_redirects=True,
        )
        self.change_cookie = ChangeCookie()
        self.headers = dict(self.base_headers)
        self.apply_session(self.change_cookie.get_session())
        print(self.redacted_headers())

    def redacted_headers(self):
        safe_headers = dict(self.headers)
        if safe_headers.get("cookie"):
            safe_headers["cookie"] = "[redacted]"
        return safe_headers

    def apply_session(self, session: dict):
        headers = dict(self.base_headers)
        extra_headers = {
            str(key).strip().lower(): str(value).strip()
            for key, value in session.get("headers", {}).items()
            if str(key).strip() and str(value).strip()
        }
        extra_headers.pop("cookie", None)
        headers.update(extra_headers)
        headers["cookie"] = session["cookie"]
        self.headers = headers

    async def get_status(self):
        return {
            "mode": "http",
            "grok_url": self.grok_url,
            "headers": self.redacted_headers(),
        }

    async def get_grok_request(self, message, model, public_base_url=None):
        data = {"message": message, "modelName": model}
        try:
            async with self.client.stream("POST", self.grok_url, headers=self.headers, json=data) as response:
                if response.status_code == 200:
                    print("200 Okay!")
                    async for line in response.aiter_lines():
                        if line:
                            try:
                                data = json.loads(line)
                                token = data.get("result", {}).get("response", {}).get("token")
                                if token:
                                    yield token
                            except json.JSONDecodeError:
                                print("\nJSON error:", line)
                    print("\n流式结束！")
                else:
                    try:
                        error_message = await response.aread()
                        self.apply_session(self.change_cookie.get_session())
                        print(self.redacted_headers())
                        print("Error:", response.status_code, error_message)
                        yield str(response.json())
                    except json.JSONDecodeError:
                        print("Error:", response.text)
        except requests.exceptions.Timeout:
            print("\n Time out!")
        except requests.exceptions.RequestException as e:
            print("\nError:", str(e))


def GrokRequest():
    mode = os.getenv("GROK_UPSTREAM_MODE", "browser").strip().lower()
    if mode == "http":
        return HttpGrokRequest()
    return BrowserGrokRequest()
