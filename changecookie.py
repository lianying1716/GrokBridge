import yaml
from pathlib import Path


class ChangeCookie:
    def __init__(self, config_path: str = "cookies.yaml"):
        self.config_path = Path(config_path)
        self.config = {}
        self.sessions = []
        self.cookie_count = 0
        self.config_mtime = None
        self._reload_config(force=True)

    def _reload_config(self, force: bool = False):
        try:
            current_mtime = self.config_path.stat().st_mtime
            if not force and self.config_mtime == current_mtime:
                return

            with self.config_path.open('r', encoding='utf-8') as file:
                self.config = yaml.safe_load(file) or {}
            self.sessions = self._build_sessions(self.config)
            self.config_mtime = current_mtime
            if self.sessions:
                self.cookie_count %= len(self.sessions)
            else:
                self.cookie_count = 0
        except Exception as e:
            print(f"读取文件时发生未知错误：{e}")
            self.config = {}
            self.sessions = []
            self.cookie_count = 0
            self.config_mtime = None

    def _normalize_headers(self, headers):
        if not isinstance(headers, dict):
            return {}
        return {
            str(key).strip().lower(): str(value).strip()
            for key, value in headers.items()
            if str(key).strip() and str(value).strip()
        }

    def _pick_stable_user_agent(self, user_agent):
        if isinstance(user_agent, list):
            for entry in user_agent:
                value = str(entry).strip()
                if value:
                    return value
            return ""
        return str(user_agent).strip() if user_agent else ""

    def _build_sessions(self, config):
        sessions = []

        configured_sessions = config.get("sessions")
        if isinstance(configured_sessions, list):
            for entry in configured_sessions:
                if not isinstance(entry, dict):
                    continue
                cookie = str(entry.get("cookie", "")).strip()
                if not cookie:
                    continue

                headers = self._normalize_headers(entry.get("headers", {}))
                user_agent = str(entry.get("user_agent", "")).strip()
                if user_agent:
                    headers.setdefault("user-agent", user_agent)

                accept_language = str(entry.get("accept_language", "")).strip()
                if accept_language:
                    headers.setdefault("accept-language", accept_language)

                sessions.append({
                    "cookie": cookie,
                    "headers": headers,
                })

        if sessions:
            return sessions

        headers = {}
        stable_user_agent = self._pick_stable_user_agent(config.get("user_agent"))
        if stable_user_agent:
            headers["user-agent"] = stable_user_agent

        accept_language = str(config.get("accept_language", "")).strip()
        if accept_language:
            headers["accept-language"] = accept_language

        cookies = config.get("cookies") or []
        return [
            {
                "cookie": str(cookie).strip(),
                "headers": dict(headers),
            }
            for cookie in cookies
            if str(cookie).strip()
        ]

    def get_session(self):
        self._reload_config()
        if not self.sessions:
            raise RuntimeError("未在 cookies.yaml 中配置可用会话")

        print(f"当前cookie: {self.cookie_count} / {len(self.sessions)}")
        session = self.sessions[self.cookie_count]
        self.cookie_count = (self.cookie_count + 1) % len(self.sessions)
        return {
            "cookie": session["cookie"],
            "headers": dict(session.get("headers", {})),
        }

    def get_sessions(self):
        self._reload_config()
        return [
            {
                "cookie": session["cookie"],
                "headers": dict(session.get("headers", {})),
            }
            for session in self.sessions
        ]

    def peek_session(self, index: int = 0):
        sessions = self.get_sessions()
        if not sessions:
            raise RuntimeError("未在 cookies.yaml 中配置可用会话")
        return sessions[index % len(sessions)]

    def get_cookie(self):
        return self.get_session()["cookie"]

    def get_user_agent(self):
        return self.get_session()["headers"].get("user-agent", "")


if __name__ == '__main__':
    test = ChangeCookie()
    print(test.get_session())
