import html as html_lib
import json
import hashlib
import re
import time
from pathlib import Path
from typing import Any, List

import requests
import uvicorn
import yaml
from fastapi import Depends, FastAPI, HTTPException, Request as FastAPIRequest
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from starlette import status
from starlette.responses import FileResponse, Response, StreamingResponse

from grok import GrokRequest


CONFIG_PATH = Path("cookies.yaml")
UI_INDEX_PATH = Path(__file__).resolve().with_name("ui") / "index.html"


class Message(BaseModel):
    role: str
    content: Any


class OpenAIRequest(BaseModel):
    model: str
    stream: bool = False
    max_tokens: int | None = None
    messages: List[Message]


class Model(BaseModel):
    id: str
    object: str = "model"
    created: int
    owned_by: str


class ModelList(BaseModel):
    object: str = "list"
    data: List[Model]


class SessionConfigInput(BaseModel):
    name: str | None = None
    cookie: str
    user_agent: str | None = None
    accept_language: str | None = None


class SaveConfigRequest(BaseModel):
    sessions: List[SessionConfigInput] | None = None
    api_key: str
    cookie: str | None = None
    user_agent: str | None = None
    accept_language: str | None = None


models_data = ModelList(
    data=[
        Model(id="grok-auto", created=int(time.time()), owned_by="xai-web"),
        Model(id="grok-fast", created=int(time.time()), owned_by="xai-web"),
        Model(id="grok-expert", created=int(time.time()), owned_by="xai-web"),
        Model(id="grok-heavy", created=int(time.time()), owned_by="xai-web"),
        Model(id="grok-latest", created=int(time.time()), owned_by="xai-web"),
        Model(id="grok-3", created=int(time.time()), owned_by="xai-web"),
    ]
)

app = FastAPI()
grok_request = GrokRequest()
security = HTTPBearer()
SESSION_SUMMARY_TTL_SECONDS = 300
SESSION_SUMMARY_CACHE: dict[str, dict[str, Any]] = {}


def read_runtime_config():
    if not CONFIG_PATH.exists():
        return {}

    try:
        return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to read config: {exc}",
        ) from exc


def write_runtime_config(config: dict):
    try:
        CONFIG_PATH.write_text(
            yaml.safe_dump(
                config,
                allow_unicode=False,
                sort_keys=False,
                default_flow_style=False,
            ),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to write config: {exc}",
        ) from exc


def normalize_api_keys(raw_passwords) -> set[str]:
    if raw_passwords is None:
        return set()
    if isinstance(raw_passwords, str):
        raw_passwords = [raw_passwords]
    return {str(password).strip() for password in raw_passwords if str(password).strip()}


def mask_secret(value: str, prefix: int = 6, suffix: int = 4):
    raw = str(value or "").strip()
    if not raw:
        return ""
    if len(raw) <= prefix + suffix:
        return "*" * len(raw)
    return f"{raw[:prefix]}********{raw[-suffix:]}"


def normalize_headers(raw_headers: Any) -> dict[str, str]:
    if not isinstance(raw_headers, dict):
        return {}
    return {
        str(key).strip().lower(): str(value).strip()
        for key, value in raw_headers.items()
        if str(key).strip() and str(value).strip()
    }


def pick_first_nonempty(raw_value: Any) -> str:
    if isinstance(raw_value, list):
        for entry in raw_value:
            value = str(entry or "").strip()
            if value:
                return value
        return ""
    return str(raw_value or "").strip()


def extract_config_sessions(config: dict) -> list[dict[str, str]]:
    sessions = []

    configured_sessions = config.get("sessions")
    if isinstance(configured_sessions, list):
        for entry in configured_sessions:
            if not isinstance(entry, dict):
                continue

            cookie = str(entry.get("cookie") or "").strip()
            if not cookie:
                continue

            headers = normalize_headers(entry.get("headers", {}))
            user_agent = str(entry.get("user_agent") or headers.get("user-agent") or "").strip()
            accept_language = str(entry.get("accept_language") or headers.get("accept-language") or "").strip()
            name = str(entry.get("name") or entry.get("label") or "").strip()

            sessions.append(
                {
                    "name": name,
                    "cookie": cookie,
                    "user_agent": user_agent,
                    "accept_language": accept_language,
                }
            )

    if sessions:
        return sessions

    shared_user_agent = pick_first_nonempty(config.get("user_agent"))
    shared_accept_language = str(config.get("accept_language") or "").strip()
    cookies = config.get("cookies") or []

    for entry in cookies:
        cookie = str(entry or "").strip()
        if not cookie:
            continue
        sessions.append(
            {
                "name": "",
                "cookie": cookie,
                "user_agent": shared_user_agent,
                "accept_language": shared_accept_language,
            }
        )

    return sessions


def serialize_ui_session(entry: dict[str, str], index: int) -> dict[str, str]:
    cookie = str(entry.get("cookie") or "").strip()
    name = str(entry.get("name") or "").strip()
    user_agent = str(entry.get("user_agent") or "").strip()
    accept_language = str(entry.get("accept_language") or "").strip()
    summary = get_session_summary(entry)
    display_name = (
        name
        or str(summary.get("email") or "").strip()
        or str(summary.get("profileName") or "").strip()
        or f"账号 {index + 1}"
    )

    return {
        "id": str(index),
        "name": name,
        "displayName": display_name,
        "cookie": cookie,
        "cookiePreview": mask_secret(cookie, prefix=14, suffix=8),
        "userAgent": user_agent,
        "acceptLanguage": accept_language,
        "summary": summary,
    }


def normalize_session_inputs(payload: SaveConfigRequest) -> list[dict[str, str]]:
    normalized_sessions = []

    if payload.sessions:
        source_items = payload.sessions
        for entry in source_items:
            cookie = str(entry.cookie or "").strip()
            if not cookie:
                continue

            normalized_sessions.append(
                {
                    "name": str(entry.name or "").strip(),
                    "cookie": cookie,
                    "user_agent": str(entry.user_agent or "").strip(),
                    "accept_language": str(entry.accept_language or "").strip(),
                }
            )
    else:
        cookie = str(payload.cookie or "").strip()
        if cookie:
            normalized_sessions.append(
                {
                    "name": "",
                    "cookie": cookie,
                    "user_agent": str(payload.user_agent or "").strip(),
                    "accept_language": str(payload.accept_language or "").strip(),
                }
            )

    return normalized_sessions


def build_session_request_headers(entry: dict[str, str]) -> dict[str, str]:
    headers = {
        "cookie": str(entry.get("cookie") or "").strip(),
        "user-agent": str(entry.get("user_agent") or "").strip() or "Mozilla/5.0",
        "accept": "*/*",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
    }

    accept_language = str(entry.get("accept_language") or "").strip()
    if accept_language:
        headers["accept-language"] = accept_language

    return headers


def get_session_cache_key(entry: dict[str, str]) -> str:
    raw = "|".join(
        [
            str(entry.get("cookie") or "").strip(),
            str(entry.get("user_agent") or "").strip(),
            str(entry.get("accept_language") or "").strip(),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_mode_capabilities(home_html: str) -> dict[str, Any]:
    default_value = {
        "availableModes": [],
        "upgradeModes": [],
        "subscriptionHint": "未知",
    }

    match = re.search(
        r'<script type="application/json" id="server-client-data-modes">(.*?)</script>',
        home_html,
        re.DOTALL,
    )
    if not match:
        return default_value

    try:
        payload = json.loads(html_lib.unescape(match.group(1)))
    except json.JSONDecodeError:
        return default_value

    available_modes = []
    upgrade_modes = []

    for mode in payload.get("modes", []):
        title = str(mode.get("title") or "").strip()
        if not title:
            continue

        availability = mode.get("availability") or {}
        if "available" in availability:
            available_modes.append(title)
        elif "requiresUpgrade" in availability:
            upgrade_modes.append(title)

    subscription_hint = "未知"
    if "Heavy" in available_modes:
        subscription_hint = "Heavy 可用"
    elif "Heavy" in upgrade_modes:
        subscription_hint = "Heavy 需升级"
    elif available_modes:
        subscription_hint = "已登录"

    return {
        "availableModes": available_modes,
        "upgradeModes": upgrade_modes,
        "subscriptionHint": subscription_hint,
    }


def parse_user_snapshot(home_html: str) -> dict[str, str]:
    anchor = home_html.find('\\"initialData\\":{\\"user\\":{')
    snippet = home_html[anchor:anchor + 8000] if anchor != -1 else home_html[:8000]

    def extract(pattern: str) -> str:
        match = re.search(pattern, snippet)
        return str(match.group(1) if match else "").strip()

    return {
        "email": extract(r'\\"email\\":\\"([^"]*)\\"'),
        "givenName": extract(r'\\"givenName\\":\\"([^"]*)\\"'),
        "familyName": extract(r'\\"familyName\\":\\"([^"]*)\\"'),
        "userId": extract(r'\\"userId\\":\\"([^"]*)\\"'),
        "xSubscriptionType": extract(r'\\"xSubscriptionType\\":\\"([^"]*)\\"'),
        "sessionTierId": extract(r'\\"sessionTierId\\":\\"([^"]*)\\"'),
        "countryCode": extract(r'\\"countryCode\\":\\"([^"]*)\\"'),
    }


def build_subscription_label(user_snapshot: dict[str, str], mode_info: dict[str, Any]) -> str:
    raw_type = str(user_snapshot.get("xSubscriptionType") or "").strip()
    if raw_type:
        return raw_type

    tier_id = str(user_snapshot.get("sessionTierId") or "").strip()
    hint = str(mode_info.get("subscriptionHint") or "").strip()
    if tier_id:
        if hint and hint != "未知":
            return f"{hint} / Tier {tier_id}"
        return f"Tier {tier_id}"

    return hint or "未知"


def summarize_session(entry: dict[str, str]) -> dict[str, Any]:
    headers = build_session_request_headers(entry)

    result = {
        "status": "unknown",
        "statusLabel": "未知",
        "email": "",
        "profileName": "",
        "countryCode": "",
        "subscriptionLabel": "未知",
        "subscriptionHint": "未知",
        "availableModes": [],
        "upgradeModes": [],
        "conversationCount": 0,
        "latestConversationTitle": "",
        "latestConversationTime": "",
        "userId": "",
        "sessionTierId": "",
    }

    home_response = requests.get(
        "https://grok.com/",
        headers={**headers, "accept": "text/html,application/xhtml+xml"},
        timeout=20,
    )
    home_response.raise_for_status()
    home_html = home_response.text

    user_snapshot = parse_user_snapshot(home_html)
    mode_info = parse_mode_capabilities(home_html)

    conversations_response = requests.get(
        "https://grok.com/rest/app-chat/conversations",
        headers=headers,
        timeout=20,
    )
    conversations_response.raise_for_status()
    conversations_payload = conversations_response.json()
    conversations = conversations_payload.get("conversations") or []
    latest_conversation = conversations[0] if conversations else {}

    full_name = " ".join(
        part
        for part in [
            str(user_snapshot.get("givenName") or "").strip(),
            str(user_snapshot.get("familyName") or "").strip(),
        ]
        if part
    ).strip()

    result.update(
        {
            "status": "ready",
            "statusLabel": "可用",
            "email": str(user_snapshot.get("email") or "").strip(),
            "profileName": full_name,
            "countryCode": str(user_snapshot.get("countryCode") or "").strip(),
            "subscriptionLabel": build_subscription_label(user_snapshot, mode_info),
            "subscriptionHint": str(mode_info.get("subscriptionHint") or "").strip(),
            "availableModes": mode_info.get("availableModes") or [],
            "upgradeModes": mode_info.get("upgradeModes") or [],
            "conversationCount": len(conversations),
            "latestConversationTitle": str(latest_conversation.get("title") or "").strip(),
            "latestConversationTime": str(latest_conversation.get("modifyTime") or "").strip(),
            "userId": str(user_snapshot.get("userId") or "").strip(),
            "sessionTierId": str(user_snapshot.get("sessionTierId") or "").strip(),
        }
    )

    return result


def get_session_summary(entry: dict[str, str]) -> dict[str, Any]:
    cache_key = get_session_cache_key(entry)
    cached = SESSION_SUMMARY_CACHE.get(cache_key)
    now = time.time()

    if cached and now - float(cached.get("timestamp") or 0) < SESSION_SUMMARY_TTL_SECONDS:
        return dict(cached.get("summary") or {})

    try:
        summary = summarize_session(entry)
    except Exception as exc:  # noqa: BLE001
        summary = {
            "status": "error",
            "statusLabel": f"获取失败: {exc}",
            "email": "",
            "profileName": "",
            "countryCode": "",
            "subscriptionLabel": "未知",
            "subscriptionHint": "未知",
            "availableModes": [],
            "upgradeModes": [],
            "conversationCount": 0,
            "latestConversationTitle": "",
            "latestConversationTime": "",
            "userId": "",
            "sessionTierId": "",
        }

    SESSION_SUMMARY_CACHE[cache_key] = {
        "timestamp": now,
        "summary": dict(summary),
    }
    return dict(summary)


def get_public_base_url(request: FastAPIRequest) -> str:
    forwarded_host = str(request.headers.get("x-forwarded-host") or "").strip()
    forwarded_proto = str(request.headers.get("x-forwarded-proto") or "").strip()
    root_path = str(request.scope.get("root_path") or "").rstrip("/")
    if forwarded_host:
        scheme = forwarded_proto or request.url.scheme
        return f"{scheme}://{forwarded_host}{root_path}".rstrip("/")
    return str(request.base_url).rstrip("/")


def build_ui_state(request: FastAPIRequest | None = None):
    config = read_runtime_config()
    sessions = extract_config_sessions(config)
    cookie_value = sessions[0]["cookie"] if sessions else ""
    api_key = config.get("password")
    if isinstance(api_key, list):
        api_key = api_key[0] if api_key else ""
    api_key = str(api_key or "").strip()

    state = {
        "configPath": str(CONFIG_PATH),
        "hasCookie": bool(cookie_value),
        "cookiePreview": mask_secret(cookie_value, prefix=14, suffix=8),
        "hasApiKey": bool(api_key),
        "apiKey": api_key,
        "apiKeyPreview": mask_secret(api_key, prefix=8, suffix=6),
        "sessionCount": len(sessions),
        "sessions": [serialize_ui_session(entry, index) for index, entry in enumerate(sessions)],
        "apiBaseUrl": "",
    }

    if request is not None:
        state["apiBaseUrl"] = f"{get_public_base_url(request)}/v1"

    return state


async def verify_api_key(authorization: HTTPAuthorizationCredentials = Depends(security)):
    config = read_runtime_config()
    valid_api_keys = normalize_api_keys(config.get("password"))
    if not valid_api_keys:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="API keys not configured"
        )

    if authorization.credentials not in valid_api_keys:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "message": "Invalid API key",
                    "type": "invalid_request_error",
                    "param": None,
                    "code": "invalid_api_key"
                }
            }
        )


async def generate_response(message: str, model: str, public_base_url: str | None = None):
    tokens = []
    async for token in grok_request.get_grok_request(str(message), model, public_base_url=public_base_url):
        tokens.append(token)
    return tokens


async def generate_stream_response(message: str, model: str, public_base_url: str | None = None):
    async for token in grok_request.get_grok_request(str(message), model, public_base_url=public_base_url):
        data = {
            "id": "grok-proxy",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "delta": {"content": token},
                    "index": 0,
                    "finish_reason": None
                }
            ]
        }
        yield f"data: {json.dumps(data)} \n\n "

    end_data = {
        "id": "grok-proxy-end",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "delta": {},
                "index": 0,
                "finish_reason": "stop"
            }
        ]
    }
    yield f"data: {json.dumps(end_data)} \n\n "
    yield "data: [DONE] \n\n "


def flatten_message_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
        return "\n".join(part for part in parts if part.strip())
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"]
        if isinstance(content.get("content"), str):
            return content["content"]
        return json.dumps(content, ensure_ascii=False)
    return str(content)


def serialize_messages(messages: List[Message]) -> str:
    lines = []
    for message in messages:
        content = flatten_message_content(message.content).strip()
        if not content:
            continue
        lines.append(f"{message.role}: {content}")
    return "\n\n".join(lines)


@app.get("/ui")
async def get_ui():
    if not UI_INDEX_PATH.exists():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="UI file missing",
        )
    return FileResponse(UI_INDEX_PATH)


@app.get("/ui/api/state")
async def get_ui_state(request: FastAPIRequest):
    return build_ui_state(request)


@app.post("/ui/api/save")
async def save_ui_config(payload: SaveConfigRequest, request: FastAPIRequest):
    api_key = str(payload.api_key or "").strip()
    sessions = normalize_session_inputs(payload)

    if not sessions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one account session is required",
        )
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="API key cannot be empty",
        )

    config = {
        "sessions": [],
        "password": api_key,
    }

    for entry in sessions:
        session_config = {"cookie": entry["cookie"]}
        if entry["name"]:
            session_config["name"] = entry["name"]
        if entry["user_agent"]:
            session_config["user_agent"] = entry["user_agent"]
        if entry["accept_language"]:
            session_config["accept_language"] = entry["accept_language"]
        config["sessions"].append(session_config)

    write_runtime_config(config)
    return {
        "ok": True,
        "message": f"Saved {len(config['sessions'])} account(s) to cookies.local.yaml mount",
        "state": build_ui_state(request),
    }


@app.get("/v1/models", response_model=ModelList, dependencies=[Depends(verify_api_key)])
async def get_models():
    return models_data


@app.get("/v1/upstream/status", dependencies=[Depends(verify_api_key)])
async def get_upstream_status():
    if hasattr(grok_request, "get_status"):
        return await grok_request.get_status()
    return {"mode": "unknown"}


@app.get("/v1/media/{media_token}")
async def get_media(media_token: str):
    if not hasattr(grok_request, "decode_media_source") or not hasattr(grok_request, "fetch_media"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Media proxy unavailable"
        )

    try:
        source = grok_request.decode_media_source(media_token)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc)
        ) from exc

    try:
        media = await grok_request.fetch_media(source)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to fetch media: {exc}"
        ) from exc

    headers = {
        "Cache-Control": "public, max-age=3600",
    }
    filename = str(media.get("filename") or "").strip()
    if filename:
        headers["Content-Disposition"] = f'inline; filename="{filename}"'

    return Response(
        content=media["content"],
        media_type=str(media.get("content_type") or "application/octet-stream"),
        headers=headers,
    )


@app.post("/v1/chat/completions", dependencies=[Depends(verify_api_key)])
async def handle_openai_request(request: OpenAIRequest, http_request: FastAPIRequest):
    prompt = serialize_messages(request.messages)
    public_base_url = get_public_base_url(http_request)
    if request.stream:
        return StreamingResponse(
            generate_stream_response(prompt, request.model, public_base_url),
            media_type="text/event-stream"
        )

    tokens = ''.join(await generate_response(prompt, request.model, public_base_url))
    return {
        "id": "grok_proxy",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": request.model,
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": tokens
                },
                "finish_reason": "stop",
                "index": 0
            }
        ]
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
