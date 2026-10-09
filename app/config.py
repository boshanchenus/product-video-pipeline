from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    data_dir: Path = Path("./data")
    minimax_api_key: str = ""
    m3_mode: str = "mock"
    m3_base_url: str = "https://api.minimax.io/v1"
    m3_api_key: str = ""
    m3_model: str = "MiniMax-M3"
    m3_timeout_seconds: float = 300.0
    m3_json_repair_attempts: int = 2
    m3_enable_review: bool = True
    m3_enable_video_review: bool = True
    m3_storyboard_max_attempts: int = 3
    m3_storyboard_pass_score: int = 70
    h3_mode: str = "mock"
    h3_base_url: str = "https://api.minimax.io"
    h3_api_key: str = ""
    h3_model: str = "MiniMax-H3"
    h3_resolution: str = "768P"
    h3_submit_path: str = "/v2/video_generation"
    h3_status_path: str = "/v2/query/video_generation/{task_id}"
    h3_enable_frame_chaining: bool = True
    h3_native_voiceover: bool = True
    h3_disable_background_music: bool = True
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    worker_poll_seconds: float = 2.0
    max_shot_attempts: int = 3
    max_upload_bytes: int = 10 * 1024 * 1024
    max_video_bytes: int = 200 * 1024 * 1024
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
