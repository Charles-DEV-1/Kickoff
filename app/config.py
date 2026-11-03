import os

from dotenv import load_dotenv

load_dotenv()


def _csv_env(name, default=""):
    return tuple(
        item.strip().rstrip("/")
        for item in os.getenv(name, default).split(",")
        if item.strip()
    )


class Settings:
    APP_ENV = os.getenv("APP_ENV", "local")
    SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
    SUPABASE_PUBLISHABLE_KEY = os.getenv("SUPABASE_PUBLISHABLE_KEY", "")
    SUPABASE_SECRET_KEY = os.getenv("SUPABASE_SECRET_KEY", "")
    # Vercel sends this only to the protected scheduled route. Server-side only.
    CRON_SECRET = os.getenv("CRON_SECRET", "")
    SUPABASE_JWKS_URL = os.getenv(
        "SUPABASE_JWKS_URL",
        f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json" if SUPABASE_URL else "",
    )
    SUPABASE_JWT_ISSUER = os.getenv(
        "SUPABASE_JWT_ISSUER",
        f"{SUPABASE_URL}/auth/v1" if SUPABASE_URL else "",
    )
    SUPABASE_JWT_AUDIENCE = os.getenv("SUPABASE_JWT_AUDIENCE", "authenticated")
    SUPABASE_JWT_ALGORITHMS = _csv_env("SUPABASE_JWT_ALGORITHMS", "ES256")
    SUPABASE_JWT_LEEWAY_SECONDS = 30
    SUPPORTED_LANGUAGES = _csv_env("SUPPORTED_LANGUAGES", "en")
    ALLOWED_ORIGINS = _csv_env("ALLOWED_ORIGINS", "")
    try:
        PUBLIC_RATE_LIMIT_TRUSTED_PROXY_HOPS = int(
            os.getenv("PUBLIC_RATE_LIMIT_TRUSTED_PROXY_HOPS", "0")
        )
    except ValueError as error:
        raise ValueError("PUBLIC_RATE_LIMIT_TRUSTED_PROXY_HOPS must be an integer") from error
    MAX_CONTENT_LENGTH = 1024 * 1024
    # Generation requires the explicit provider setting and its server-side key.
    AI_PROVIDER = os.getenv("AI_PROVIDER", "disabled").strip().lower()
    GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
    GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
    AI_DAILY_ALLOWANCE = int(os.getenv("AI_DAILY_ALLOWANCE", "10"))
    AI_PROVIDER_DAILY_BUDGET = int(os.getenv("AI_PROVIDER_DAILY_BUDGET", "1000"))
    AI_CACHE_TTL_SECONDS = int(os.getenv("AI_CACHE_TTL_SECONDS", "86400"))
    AI_CACHE_HMAC_KEY = os.getenv("AI_CACHE_HMAC_KEY", "")
    AI_PROVIDER_TIMEOUT_SECONDS = float(os.getenv("AI_PROVIDER_TIMEOUT_SECONDS", "5"))
    AI_TOTAL_BUDGET_SECONDS = float(os.getenv("AI_TOTAL_BUDGET_SECONDS", "8"))
    JSON_SORT_KEYS = False
    PROPAGATE_EXCEPTIONS = False
