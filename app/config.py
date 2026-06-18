from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    mistral_api_key: str = ""
    github_token: str = ""
    github_webhook_secret: str = ""
    slack_bot_token: str = ""
    slack_signing_secret: str = ""
    database_url: str = "sqlite+aiosqlite:///./hydra.db"
    max_ci_iterations: int = 3
    vibe_max_turns: int = 50
    vibe_max_price: float = 5.0


settings = Settings()
