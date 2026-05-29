import os
from typing import Dict, List, Optional


class Settings:
    _instance: Optional["Settings"] = None

    def __init__(self):
        self.app_name: str = os.getenv("SCHEDULER_APP_NAME", "OpenClaw Model Scheduler")
        self.host: str = os.getenv("SCHEDULER_HOST", "0.0.0.0")
        self.port: int = int(os.getenv("SCHEDULER_PORT", "8000"))
        self.log_level: str = os.getenv("SCHEDULER_LOG_LEVEL", "INFO")
        self.openclaw_gateway_url: str = os.getenv("OPENCLAW_GATEWAY_URL", "http://localhost:3000")
        self.openclaw_api_key: Optional[str] = os.getenv("OPENCLAW_API_KEY")
        self.rate_limit_rpm: int = int(os.getenv("RATE_LIMIT_RPM", "60"))
        self.rate_limit_per_appid: int = int(os.getenv("RATE_LIMIT_PER_APPID", "30"))
        self.max_retries: int = int(os.getenv("MAX_RETRIES", "2"))
        self.default_timeout_ms: int = int(os.getenv("DEFAULT_TIMEOUT_MS", "30000"))

    @classmethod
    def get_instance(cls) -> "Settings":
        if cls._instance is None:
            cls._instance = Settings()
        return cls._instance


def get_settings() -> Settings:
    return Settings.get_instance()
