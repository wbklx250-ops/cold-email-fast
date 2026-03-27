from functools import lru_cache
import json

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False)

    database_url: str
    secret_key: str
    debug: bool = False

    cloudflare_api_key: str | None = None
    cloudflare_email: str | None = None
    cloudflare_account_id: str | None = None

    # Secondary Cloudflare account (for multi-account zone lookup)
    cloudflare_api_key_2: str | None = None
    cloudflare_email_2: str | None = None
    cloudflare_account_id_2: str | None = None

    azure_client_id: str | None = None
    azure_client_secret: str | None = None
    azure_tenant_id: str | None = None
    azure_automation_webhook_url: str | None = None
    railway_public_url: str | None = None
    
    # Microsoft Graph API - Device Code Flow
    MS_CLIENT_ID: str = ""  # From Azure App Registration

    # Security
    encryption_key: str = ""  # For encrypting sensitive data
    
    # Selenium
    screenshot_dir: str = "/tmp/screenshots"  # Directory for browser screenshots
    
    # Parallel Processing for Step 5/6
    # Controls how many browser instances run simultaneously
    # 6 workers × ~300MB per Chrome = ~1.8GB RAM needed
    # Railway has upgraded memory to support this
    max_parallel_browsers: int = 20
    
    # Step 5 Browser Display Mode
    # False = visible browsers (useful for debugging/testing)
    # True = headless mode (faster, no GUI, REQUIRED for production/Railway - no display available!)
    step5_headless: bool = True
    
    # Step 6 Browser Display Mode (Mailbox creation via Selenium + PowerShell)
    # False = visible browsers (useful for debugging/testing)
    # True = headless mode (REQUIRED for production/Railway - no display available!)
    step6_headless: bool = True

    # Domain Checker parallel browsers
    # Lighter than Step 5 (read-only), so can be slightly more aggressive
    # 3 = safe default (~1.1GB RAM), 4-5 for larger Railway instances
    checker_parallel_browsers: int = 2

    # Headless stability delays (seconds)
    # Increase in headless to avoid racing Microsoft login screens
    headless_delay_seconds: float = 1.5
    headless_page_settle_seconds: float = 2.0

    allowed_origins: str = ""
    
    @property
    def allowed_origins_list(self) -> list[str]:
        """Parse allowed_origins as comma-separated string or JSON array."""
        if not self.allowed_origins:
            return []
        # Handle both comma-separated and JSON array formats
        if self.allowed_origins.startswith('['):
            return json.loads(self.allowed_origins)
        return [origin.strip() for origin in self.allowed_origins.split(',') if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()