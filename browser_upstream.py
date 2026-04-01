import asyncio
import base64
import json
import mimetypes
import os
import re
import socket
import time

from http.cookies import SimpleCookie
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.request import urlopen

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright
import requests

from changecookie import ChangeCookie


class BrowserGrokRequest:
    ASSET_BASE_URLS = (
        "https://assets.grok.com/",
        "https://assets.grokusercontent.com/",
    )
    GROK_RENDER_RE = re.compile(r"<grok:render\b.*?</grok:render>", re.IGNORECASE | re.DOTALL)
    XAI_TOOL_CARD_RE = re.compile(r"<xai:tool_usage_card\b.*?</xai:tool_usage_card>", re.IGNORECASE | re.DOTALL)
    MODEL_ALIASES = {
        "auto": "Auto",
        "default": "Auto",
        "grok-auto": "Auto",
        "grok-latest": "Auto",
        "grok-4-auto": "Auto",
        "fast": "Fast",
        "grok-fast": "Fast",
        "grok-3": "Fast",
        "expert": "Expert",
        "grok-expert": "Expert",
        "grok-4": "Expert",
        "heavy": "Heavy",
        "grok-heavy": "Heavy",
        "grok-4-heavy": "Heavy",
    }

    def __init__(self):
        self.change_cookie = ChangeCookie()
        self.cdp_url = os.getenv("GROK_BROWSER_CDP_URL", "http://browser:9222")
        self.home_url = os.getenv("GROK_HOME_URL", "https://grok.com/")
        self.chat_path = os.getenv("GROK_CHAT_PATH", "/rest/app-chat/conversations/new")
        self.connect_timeout = int(os.getenv("GROK_BROWSER_CONNECT_TIMEOUT", "90"))
        self.page_wait_ms = int(os.getenv("GROK_BROWSER_PAGE_WAIT_MS", "30000"))
        self.fetch_timeout_ms = int(os.getenv("GROK_BROWSER_FETCH_TIMEOUT_MS", "240000"))
        self._resolved_cdp_url = None

        self._lock = asyncio.Lock()
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._binding_ready = False
        self._session_signature = None
        self._queue = None

    def _parse_cookie_header(self, cookie_header: str):
        jar = SimpleCookie()
        jar.load(cookie_header)
        cookies = []
        for morsel in jar.values():
            cookies.append(
                {
                    "name": morsel.key,
                    "value": morsel.value,
                    "url": self.home_url,
                }
            )
        return cookies

    def _session_signature_from(self, session: dict):
        return json.dumps(session, sort_keys=True, ensure_ascii=False)

    async def _push_to_queue(self, _source, payload):
        if self._queue is not None:
            await self._queue.put(payload)

    def _fetch_cdp_ws_url(self):
        requested = urlparse(self.cdp_url)
        connect_host = requested.hostname or "127.0.0.1"
        connect_ip = socket.gethostbyname(connect_host)
        connect_port = requested.port

        if connect_port:
            connect_netloc = f"{connect_ip}:{connect_port}"
        else:
            connect_netloc = connect_ip

        version_url = urlunparse(requested._replace(netloc=connect_netloc, path="/json/version"))
        with urlopen(version_url, timeout=min(self.connect_timeout, 10)) as response:
            payload = json.loads(response.read().decode("utf-8"))

        ws_url = str(payload.get("webSocketDebuggerUrl", "")).strip()
        if not ws_url:
            raise RuntimeError(f"CDP 端点未返回 websocket 地址: {version_url}")

        resolved = urlparse(ws_url)
        if resolved.hostname in {"127.0.0.1", "localhost"}:
            resolved = resolved._replace(netloc=connect_netloc)
            ws_url = urlunparse(resolved)

        return ws_url

    async def _connect_browser(self):
        if self._playwright is None:
            self._playwright = await async_playwright().start()

        if self._browser is not None and self._browser.is_connected():
            return

        last_error = None
        deadline = time.monotonic() + self.connect_timeout
        while time.monotonic() < deadline:
            try:
                resolved_cdp_url = await asyncio.to_thread(self._fetch_cdp_ws_url)
                self._browser = await self._playwright.chromium.connect_over_cdp(resolved_cdp_url)
                self._resolved_cdp_url = resolved_cdp_url
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                await asyncio.sleep(2)

        raise RuntimeError(f"无法连接浏览器 CDP: {last_error}")

    async def _ensure_page(self):
        await self._connect_browser()

        if not self._browser.contexts:
            raise RuntimeError("浏览器已连接，但没有可用的默认上下文")

        self._context = self._browser.contexts[0]
        if self._page is None or self._page.is_closed():
            preferred_page = next(
                (page for page in self._context.pages if page.url.startswith("https://grok.com")),
                None,
            )
            self._page = preferred_page or (self._context.pages[0] if self._context.pages else await self._context.new_page())
            self._binding_ready = False

        if not self._binding_ready:
            try:
                await self._page.expose_binding("__grok_bridge_push", self._push_to_queue)
            except PlaywrightError as exc:
                if "has been already registered" not in str(exc):
                    raise
            self._binding_ready = True

        session = self.change_cookie.peek_session(0)
        signature = self._session_signature_from(session)
        session_updated = False
        if signature != self._session_signature:
            cookies = self._parse_cookie_header(session["cookie"])
            if cookies:
                await self._context.add_cookies(cookies)

            extra_headers = {
                key: value
                for key, value in session.get("headers", {}).items()
                if key not in {"cookie", "user-agent"}
            }
            if extra_headers:
                await self._page.set_extra_http_headers(extra_headers)

            self._session_signature = signature
            session_updated = True

        if session_updated or not self._page.url.startswith("https://grok.com"):
            if self._page.url.startswith("https://grok.com"):
                await self._page.reload(wait_until="domcontentloaded", timeout=self.page_wait_ms)
            else:
                await self._page.goto(self.home_url, wait_until="domcontentloaded", timeout=self.page_wait_ms)
            await self._page.wait_for_timeout(1500)

    async def get_status(self):
        async with self._lock:
            await self._ensure_page()
            title = await self._page.title()
            cookies = await self._context.cookies(self.home_url)
            selected_mode = ""
            try:
                selected_mode = (await self._page.get_by_role("button", name="Model select").inner_text()).strip()
            except Exception:  # noqa: BLE001
                selected_mode = ""
            cookie_names = sorted(cookie["name"] for cookie in cookies if cookie["name"] in {"sso", "sso-rw", "cf_clearance", "__cf_bm"})
            return {
                "mode": "browser",
                "cdp_url": self.cdp_url,
                "resolved_cdp_url": self._resolved_cdp_url,
                "page_url": self._page.url,
                "page_title": title,
                "selected_model_mode": selected_mode,
                "session_cookies": cookie_names,
                "browser_connected": self._browser.is_connected() if self._browser is not None else False,
            }

    @staticmethod
    def encode_media_source(source: str) -> str:
        return base64.urlsafe_b64encode(source.encode("utf-8")).decode("ascii").rstrip("=")

    @staticmethod
    def decode_media_source(token: str) -> str:
        padding = "=" * (-len(token) % 4)
        try:
            decoded = base64.urlsafe_b64decode(f"{token}{padding}")
            value = decoded.decode("utf-8").strip()
        except Exception as exc:  # noqa: BLE001
            raise ValueError("无效的媒体令牌") from exc

        if not value:
            raise ValueError("无效的媒体令牌")
        return value

    def build_public_media_url(self, source: str, public_base_url: str | None):
        normalized_base_url = str(public_base_url or "").strip().rstrip("/")
        if not normalized_base_url:
            parsed = urlparse(str(source).strip())
            return str(source).strip() if parsed.scheme in {"http", "https"} else ""
        return f"{normalized_base_url}/v1/media/{self.encode_media_source(source)}"

    def _parse_upstream_lines(self, response_text: str):
        events = []
        for raw_line in response_text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    def _strip_special_markup(self, text: str):
        cleaned = self.GROK_RENDER_RE.sub("", text or "")
        cleaned = self.XAI_TOOL_CARD_RE.sub("", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    def _decode_card_attachment(self, raw_attachment):
        if isinstance(raw_attachment, dict):
            return raw_attachment
        if not isinstance(raw_attachment, str):
            return None
        try:
            decoded = json.loads(raw_attachment)
        except json.JSONDecodeError:
            return None
        return decoded if isinstance(decoded, dict) else None

    def _extract_image_entries(self, card_attachments):
        images = {}

        for raw_attachment in card_attachments:
            attachment = self._decode_card_attachment(raw_attachment)
            if not attachment:
                continue

            card_type = str(attachment.get("cardType") or attachment.get("type") or "").strip()

            if card_type == "image_card":
                image = attachment.get("image")
                if not isinstance(image, dict):
                    continue

                original_url = str(image.get("original") or image.get("thumbnail") or "").strip()
                if not original_url:
                    continue

                images[original_url] = {
                    "kind": "search",
                    "source": original_url,
                    "title": str(image.get("title") or image.get("source") or "图片").strip(),
                    "source_page": str(image.get("link") or "").strip(),
                }
                continue

            if card_type != "generated_image_card":
                continue

            image_chunk = attachment.get("image_chunk")
            if not isinstance(image_chunk, dict):
                continue

            image_url = str(image_chunk.get("imageUrl") or "").strip()
            if not image_url:
                continue

            try:
                progress = int(image_chunk.get("progress") or 0)
            except (TypeError, ValueError):
                progress = 0

            key = str(image_chunk.get("imageUuid") or image_url).strip()
            existing = images.get(key)
            if existing and progress < int(existing.get("progress") or 0):
                continue

            image_prompt = image_chunk.get("imagePrompt")
            prompt = ""
            if isinstance(image_prompt, dict):
                prompt = str(image_prompt.get("prompt") or "").strip()

            images[key] = {
                "kind": "generated",
                "source": image_url,
                "title": str(image_chunk.get("imageTitle") or "Generated Image").strip(),
                "progress": progress,
                "prompt": prompt,
            }

        return list(images.values())

    def _collect_model_response(self, response_text: str):
        events = self._parse_upstream_lines(response_text)
        model_response = None
        fallback_tokens = []
        fallback_attachments = []

        for event in events:
            response = event.get("result", {}).get("response", {})
            if not isinstance(response, dict):
                continue

            token = response.get("token")
            if isinstance(token, str):
                fallback_tokens.append(token)

            card_attachment = response.get("cardAttachment")
            if isinstance(card_attachment, dict):
                json_data = card_attachment.get("jsonData")
                if isinstance(json_data, str) and json_data.strip():
                    fallback_attachments.append(json_data)

            candidate = response.get("modelResponse")
            if isinstance(candidate, dict):
                model_response = candidate

        if not model_response:
            return {
                "text": self._strip_special_markup("".join(fallback_tokens)),
                "images": self._extract_image_entries(fallback_attachments),
            }

        message_text = str(model_response.get("message") or "").strip()
        if not message_text:
            message_text = "".join(fallback_tokens)

        card_attachments = model_response.get("cardAttachmentsJson")
        if not isinstance(card_attachments, list):
            card_attachments = fallback_attachments

        images = self._extract_image_entries(card_attachments)
        cleaned_text = self._strip_special_markup(message_text)
        if images and not cleaned_text:
            cleaned_text = "已找到图片结果。"

        return {
            "text": cleaned_text,
            "images": images,
        }

    def _format_response_text(self, response_text: str, public_base_url: str | None = None):
        parsed_response = self._collect_model_response(response_text)
        text = parsed_response.get("text", "").strip()
        images = parsed_response.get("images", [])

        if not images:
            return text

        lines = [text or "已找到图片结果。"]
        for index, image in enumerate(images, start=1):
            media_url = self.build_public_media_url(str(image.get("source") or ""), public_base_url)
            if not media_url:
                continue

            lines.append("")
            lines.append(f"图片{index}: {str(image.get('title') or '图片').strip()}")
            lines.append(media_url)

            source_page = str(image.get("source_page") or "").strip()
            if source_page:
                lines.append(f"来源: {source_page}")

            lines.append(f"MEDIA:{media_url}")

        return "\n".join(lines).strip()

    def _resolve_media_candidates(self, source: str):
        normalized = str(source or "").strip()
        if not normalized:
            return []

        parsed = urlparse(normalized)
        if parsed.scheme in {"http", "https"}:
            return [normalized]

        media_path = normalized.lstrip("/")
        return [urljoin(base_url, media_path) for base_url in self.ASSET_BASE_URLS]

    def _infer_filename(self, url: str, content_type: str):
        filename = os.path.basename(urlparse(url).path) or "image"
        if "." not in filename:
            extension = mimetypes.guess_extension(content_type) or ""
            filename = f"{filename}{extension}"
        return filename

    def _download_media(self, source: str):
        session = self.change_cookie.peek_session(0)
        base_headers = {
            key: value
            for key, value in session.get("headers", {}).items()
            if key != "cookie"
        }
        last_error = None

        for candidate_url in self._resolve_media_candidates(source):
            headers = dict(base_headers)
            host = (urlparse(candidate_url).hostname or "").lower()
            if host.endswith("grok.com") or host.endswith("grokusercontent.com"):
                headers["cookie"] = session["cookie"]
                headers.setdefault("referer", self.home_url)
                headers.setdefault("origin", self.home_url.rstrip("/"))

            try:
                response = requests.get(
                    candidate_url,
                    headers=headers,
                    timeout=60,
                    allow_redirects=True,
                )
                response.raise_for_status()
                content_type = (
                    response.headers.get("content-type", "application/octet-stream").split(";", 1)[0].strip()
                    or "application/octet-stream"
                )
                return {
                    "content": response.content,
                    "content_type": content_type,
                    "filename": self._infer_filename(response.url or candidate_url, content_type),
                }
            except Exception as exc:  # noqa: BLE001
                last_error = exc

        raise RuntimeError(f"下载媒体失败: {last_error}")

    async def fetch_media(self, source: str):
        return await asyncio.to_thread(self._download_media, source)

    async def _run_fetch(self, payload):
        script = """
async ({ path, payload, timeoutMs }) => {
  const push = async (item) => {
    if (window.__grok_bridge_push) {
      await window.__grok_bridge_push(item);
    }
  };

  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort("fetch-timeout"), timeoutMs);

    const response = await fetch(path, {
      method: "POST",
      credentials: "include",
      headers: {
        "accept": "*/*",
        "content-type": "application/json"
      },
      body: JSON.stringify(payload),
      signal: controller.signal
    });

    clearTimeout(timer);
    await push({
      type: "meta",
      status: response.status,
      url: response.url,
      contentType: response.headers.get("content-type") || ""
    });

    if (!response.body) {
      await push({ type: "chunk", data: await response.text() });
      await push({ type: "done" });
      return;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      const chunk = decoder.decode(value, { stream: true });
      if (chunk) {
        await push({ type: "chunk", data: chunk });
      }
    }

    const tail = decoder.decode();
    if (tail) {
      await push({ type: "chunk", data: tail });
    }

    await push({ type: "done" });
  } catch (error) {
    await push({ type: "error", error: String(error) });
  }
}
"""
        await self._page.evaluate(
            script,
            {
                "path": self.chat_path,
                "payload": payload,
                "timeoutMs": self.fetch_timeout_ms,
            },
        )

    async def _submit_prompt_via_ui(self, message: str, model: str):
        await self._page.goto(self.home_url, wait_until="domcontentloaded", timeout=self.page_wait_ms)
        await self._page.wait_for_timeout(1500)
        await self._select_model_mode(model)

        editor = self._page.locator('.ProseMirror[contenteditable="true"]').first
        await editor.wait_for(state="visible", timeout=self.page_wait_ms)
        await editor.click()
        await self._page.keyboard.press("Control+A")
        await self._page.keyboard.press("Backspace")
        await self._page.keyboard.insert_text(message)
        await self._page.wait_for_timeout(300)

        submit = self._page.get_by_role("button", name="Submit")
        deadline = time.monotonic() + 15
        while await submit.is_disabled():
            if time.monotonic() >= deadline:
                raise RuntimeError("页面提交按钮未激活，无法发送消息")
            await self._page.wait_for_timeout(200)

        async with self._page.expect_response(
            lambda response: response.request.method == "POST" and response.url.endswith(self.chat_path),
            timeout=self.fetch_timeout_ms,
        ) as response_info:
            await submit.click()

        return await response_info.value

    def _resolve_model_mode(self, model: str) -> str:
        normalized = str(model).strip().lower()
        return self.MODEL_ALIASES.get(normalized, "Auto")

    async def _select_model_mode(self, model: str):
        target_mode = self._resolve_model_mode(model)
        button = self._page.get_by_role("button", name="Model select")
        await button.wait_for(state="visible", timeout=self.page_wait_ms)

        current_mode = (await button.inner_text()).strip()
        if current_mode.lower() == target_mode.lower():
            return target_mode

        await button.click(force=True)
        option = self._page.get_by_role("menuitem", name=re.compile(rf"^{re.escape(target_mode)}", re.I)).first
        await option.wait_for(state="visible", timeout=self.page_wait_ms)
        await option.click(force=True)
        await self._page.wait_for_timeout(400)

        selected_mode = (await self._page.get_by_role("button", name="Model select").inner_text()).strip()
        if selected_mode.lower() != target_mode.lower():
            raise RuntimeError(f"模型切换失败，期望 {target_mode}，实际 {selected_mode or 'unknown'}")

        return target_mode

    async def get_grok_request(self, message, model, public_base_url=None):
        async with self._lock:
            await self._ensure_page()
            response = await self._submit_prompt_via_ui(message, model)
            status_code = response.status
            response_text = await response.text()

            if status_code != 200:
                yield response_text.strip() or json.dumps(
                    {
                        "error": {
                            "message": "Browser upstream request failed",
                            "status": status_code,
                        }
                    }
                )
                return

            formatted_text = self._format_response_text(response_text, public_base_url)
            if formatted_text:
                yield formatted_text
