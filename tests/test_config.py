from app.config import Settings


async def test_default_settings():
    s = Settings(
        _env_file=None,
        mistral_api_key="test-key",
        github_token="gh-token",
    )
    assert s.database_url == "sqlite+aiosqlite:///./hydra.db"
    assert s.max_ci_iterations == 3
    assert s.vibe_max_turns == 50
    assert s.vibe_max_price == 5.0
    assert s.mistral_api_key == "test-key"
