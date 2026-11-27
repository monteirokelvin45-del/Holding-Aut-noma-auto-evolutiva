"""Configuração central tipada da Automaton Holding Corp.

Lê variáveis de ambiente (ou ``.env``) uma única vez, valida invariantes de
segurança e expõe um objecto imutável ``settings`` para todo o backend.

Regras não negociáveis aplicadas aqui:
  * Capital inicial zero: os limites de gasto arrancam em ``0.00`` quando não
    definidos e nunca podem ser negativos.
  * Nenhuma chave é obrigatória para o sistema arrancar — sem chaves o roteador
    cai no motor heurístico local e os conectores externos entram em dry-run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]

_TRUTHY = {"1", "true", "yes", "on", "sim", "y", "t"}


def _load_dotenv_file(path: Path) -> None:
    """Carrega um ficheiro ``.env`` simples sem dependências externas.

    Só define variáveis que ainda não existam no ambiente real, para que
    Docker/CI tenham sempre precedência. Nunca falha: ficheiros ausentes ou
    malformados são ignorados linha a linha.
    """
    if not path.is_file():
        return
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        if key not in os.environ:
            os.environ[key] = value


_load_dotenv_file(REPO_ROOT / ".env")


def env_str(name: str, default: str = "") -> str:
    value = os.environ.get(name, default)
    return value.strip() if isinstance(value, str) else default


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUTHY


def env_int(name: str, default: int, minimum: Optional[int] = None) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(float(raw.strip()))
    except (TypeError, ValueError):
        return default
    if minimum is not None and value < minimum:
        return minimum
    return value


def env_float(name: str, default: float, minimum: Optional[float] = None) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw.strip().replace(",", "."))
    except (TypeError, ValueError):
        return default
    if minimum is not None and value < minimum:
        return minimum
    return value


def env_list(name: str, default: Optional[List[str]] = None) -> List[str]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return list(default or [])
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    """Configuração imutável da holding (lida uma vez no import)."""

    # ---------------------------------------------------------------- ambiente
    app_env: str = "production"
    log_level: str = "INFO"
    timezone: str = "Africa/Maputo"
    host: str = "0.0.0.0"
    port: int = 8000
    secret_key: str = "insecure-development-key-change-me"
    api_access_token: str = ""
    # Intervalo do heartbeat do /ws/live. 25 s mantém viva a ligação atrás de
    # proxies; em testes baixa-se para não esperar meio minuto por ligação.
    ws_heartbeat_seconds: float = 25.0
    cors_origins: List[str] = field(default_factory=lambda: ["*"])

    # -------------------------------------------------------------------- vault
    vault_dir: Path = field(default_factory=lambda: REPO_ROOT / "vault_data")
    daily_spend_cap_usd: float = 0.0
    per_agent_daily_cap_usd: float = 0.0
    approval_threshold_usd: float = 5.0
    base_currency: str = "USD"
    usd_mzn_rate: float = 63.5

    # --------------------------------------------------------------------- LLM
    groq_api_key: str = ""
    groq_model_reasoning: str = "gpt-oss-120b"
    groq_model_fast: str = "qwen3-27b"
    groq_rpm_limit: int = 30
    groq_rpd_limit: int = 1000
    groq_tpd_limit: int = 200_000

    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_rpm_limit: int = 10
    gemini_rpd_limit: int = 250
    gemini_tpd_limit: int = 250_000

    openrouter_api_key: str = ""
    openrouter_model: str = "meta-llama/llama-3.3-70b-instruct:free"
    openrouter_rpm_limit: int = 20
    openrouter_rpd_limit: int = 50
    openrouter_rpd_limit_upgraded: int = 1000

    ollama_base_url: str = "http://ollama:11434"
    ollama_model: str = "llama3.2:3b"
    ollama_enabled: bool = False

    llm_provider_order: List[str] = field(
        default_factory=lambda: ["groq", "gemini", "openrouter", "ollama", "local"]
    )
    #: Fracção do limite de um provedor a partir da qual se muda de rota, antes
    #: de bater no 429. 0.85 = 85% do RPM/RPD/TPD mais apertado.
    llm_failover_threshold: float = 0.85

    # -------------------------------------------------------- observabilidade
    langfuse_enabled: bool = False
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "http://langfuse:3000"

    # ---------------------------------------------------------------- telegram
    telegram_bot_token: str = ""
    telegram_allowed_chat_ids: List[str] = field(default_factory=list)
    telegram_polling_enabled: bool = False

    # ---------------------------------------------------------------- segurança
    llm_guard_enabled: bool = True
    llm_guard_fail_closed: bool = True
    prompt_injection_block_score: float = 0.85
    max_untrusted_content_chars: int = 12_000

    # ------------------------------------------------------------ scout / web
    crawl4ai_enabled: bool = False
    gpt_researcher_enabled: bool = False
    scout_sources: List[str] = field(default_factory=list)
    scout_interval_minutes: int = 360
    scout_user_agent: str = "AutomatonHoldingBot/1.0 (+research)"

    # --------------------------------------------------------------- validação
    smoke_min_clicks: int = 25
    smoke_min_conversion_rate: float = 0.04
    smoke_min_days: int = 3
    smoke_max_days: int = 7
    eval_min_score: float = 85.0
    eval_max_retries: int = 3

    # ------------------------------------------------------------ mídia / vídeo
    pexels_api_key: str = ""
    pixabay_api_key: str = ""
    ffmpeg_binary: str = "ffmpeg"
    video_output_dir: Path = field(
        default_factory=lambda: REPO_ROOT / "vault_data" / "media" / "videos"
    )
    video_target_width: int = 1080
    video_target_height: int = 1920
    video_target_fps: int = 30
    video_max_duration_seconds: int = 58
    tts_provider: str = "edge_tts"
    tts_voice_pt: str = "pt-PT-RaquelNeural"
    tts_voice_en: str = "en-US-AriaNeural"
    moneyprinter_base_url: str = "http://moneyprinter:8080"
    shortgpt_enabled: bool = False
    youtube_shorts_generator_enabled: bool = False
    # --- geração de footage/imagem por IA aberta (custo $0.00, local) -------
    genai_video_enabled: bool = False
    genai_video_order: List[str] = field(
        default_factory=lambda: ["wan22", "longcat", "kandinsky", "ltx", "sdxl"]
    )
    genai_timeout_seconds: int = 600
    wan22_endpoint: str = ""
    longcat_endpoint: str = ""
    kandinsky_endpoint: str = ""
    ltx_endpoint: str = ""
    sdxl_endpoint: str = ""
    # --- pesquisa profunda (Crawl4AI / GPT-Researcher) -----------------------
    research_max_results: int = 5
    research_timeout_seconds: int = 240
    # --- pesquisa profunda: quantos resultados o Scout deve recolher ---------
    # (o bot Telegram usa ``telegram_polling_enabled``, definido acima)

    # ------------------------------------------------------------------ negócios
    gumroad_access_token: str = ""
    gumroad_email: str = ""
    lemonsqueezy_api_key: str = ""
    lemonsqueezy_store_id: str = ""
    amazon_associate_tag: str = ""
    hotmart_affiliate_id: str = ""
    shopee_affiliate_id: str = ""
    adsense_client_id: str = ""
    wordpress_base_url: str = ""
    wordpress_app_password: str = ""
    github_token: str = ""
    github_pages_base_url: str = "https://automaton-holding.github.io"

    # ------------------------------------------------------------------ financeiro
    tax_reserve_rate: float = 0.05
    mpesa_base_url: str = "https://api.sandbox.vm.co.mz"
    mpesa_api_key: str = ""
    mpesa_public_key: str = ""
    mpesa_service_provider_code: str = ""
    mpesa_origin: str = "+258840000000"
    mpesa_initiator_identifier: str = ""
    mpesa_initiator_password: str = ""
    mpesa_security_credential: str = ""
    mpesa_receiver_msisdn: str = "+258840000000"
    mpesa_sandbox: bool = True
    emola_base_url: str = "https://api.emola.co.mz"
    emola_api_key: str = ""
    emola_merchant_code: str = ""
    emola_receiver_msisdn: str = "+258860000000"
    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    paypal_mode: str = "sandbox"
    wise_api_token: str = ""
    wise_profile_id: str = ""
    wise_default_target_currency: str = "MZN"

    # --------------------------------------------------------------- RH / juiz
    hr_min_success_rate: float = 0.60
    hr_max_cost_per_success_usd: float = 0.0
    judge_min_score: float = 85.0

    # ------------------------------------------------------------------ helpers
    @property
    def vault_path(self) -> Path:
        return Path(self.vault_dir)

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    def configured_secrets(self) -> Dict[str, bool]:
        """Mapa de quais integrações têm credenciais (útil para /health)."""
        return {
            "groq": bool(self.groq_api_key),
            "gemini": bool(self.gemini_api_key),
            "openrouter": bool(self.openrouter_api_key),
            "ollama": bool(self.ollama_enabled),
            "langfuse": bool(self.langfuse_enabled and self.langfuse_public_key),
            "telegram": bool(self.telegram_bot_token),
            "pexels": bool(self.pexels_api_key),
            "pixabay": bool(self.pixabay_api_key),
            "genai_video": bool(self.genai_video_enabled and any([
                self.wan22_endpoint, self.longcat_endpoint,
                self.kandinsky_endpoint, self.ltx_endpoint, self.sdxl_endpoint,
            ])),
            "gumroad": bool(self.gumroad_access_token),
            "lemonsqueezy": bool(self.lemonsqueezy_api_key),
            "mpesa": bool(self.mpesa_api_key and self.mpesa_public_key),
            "emola": bool(self.emola_api_key),
            "paypal": bool(self.paypal_client_id and self.paypal_client_secret),
            "wise": bool(self.wise_api_token),
            "github": bool(self.github_token),
        }

    def safe_summary(self) -> Dict[str, object]:
        """Resumo sem segredos, para expor em endpoints públicos."""
        return {
            "app_env": self.app_env,
            "phase_caps": {
                "daily_spend_cap_usd": self.daily_spend_cap_usd,
                "per_agent_daily_cap_usd": self.per_agent_daily_cap_usd,
                "approval_threshold_usd": self.approval_threshold_usd,
            },
            "llm_provider_order": list(self.llm_provider_order),
            "llm_failover_threshold": self.llm_failover_threshold,
            "genai_video": {
                "enabled": self.genai_video_enabled,
                "order": list(self.genai_video_order),
            },
            "tts_provider": self.tts_provider,
            "integrations": self.configured_secrets(),
            "eval_min_score": self.eval_min_score,
            "kill_policy": {
                "min_traction_days": 7,
                "first_dollar_deadline_days": 21,
            },
        }


def _build_settings() -> Settings:
    vault_env = env_str("VAULT_DIR", "./vault_data")
    vault_dir = Path(vault_env)
    if not vault_dir.is_absolute():
        vault_dir = (REPO_ROOT / vault_dir).resolve()

    video_env = env_str("VIDEO_OUTPUT_DIR", "./vault_data/media/videos")
    video_dir = Path(video_env)
    if not video_dir.is_absolute():
        video_dir = (REPO_ROOT / video_dir).resolve()

    return Settings(
        app_env=env_str("APP_ENV", "production"),
        log_level=env_str("LOG_LEVEL", "INFO").upper(),
        timezone=env_str("TZ", "Africa/Maputo"),
        host=env_str("HOST", "0.0.0.0"),
        port=env_int("PORT", 8000, minimum=1),
        secret_key=env_str("SECRET_KEY", "insecure-development-key-change-me"),
        api_access_token=env_str("API_ACCESS_TOKEN", ""),
        ws_heartbeat_seconds=env_float("WS_HEARTBEAT_SECONDS", 25.0, minimum=0.1),
        cors_origins=env_list("CORS_ORIGINS", ["*"]),
        vault_dir=vault_dir,
        daily_spend_cap_usd=env_float("DAILY_SPEND_CAP_USD", 0.0, minimum=0.0),
        per_agent_daily_cap_usd=env_float("PER_AGENT_DAILY_CAP_USD", 0.0, minimum=0.0),
        approval_threshold_usd=env_float("APPROVAL_THRESHOLD_USD", 5.0, minimum=0.0),
        base_currency=env_str("BASE_CURRENCY", "USD"),
        usd_mzn_rate=env_float("USD_MZN_RATE", 63.5, minimum=0.01),
        groq_api_key=env_str("GROQ_API_KEY"),
        groq_model_reasoning=env_str("GROQ_MODEL_REASONING", "gpt-oss-120b"),
        groq_model_fast=env_str("GROQ_MODEL_FAST", "qwen3-27b"),
        groq_rpm_limit=env_int("GROQ_RPM_LIMIT", 30, minimum=1),
        groq_rpd_limit=env_int("GROQ_RPD_LIMIT", 1000, minimum=1),
        groq_tpd_limit=env_int("GROQ_TPD_LIMIT", 200_000, minimum=1),
        gemini_api_key=env_str("GEMINI_API_KEY"),
        gemini_model=env_str("GEMINI_MODEL", "gemini-2.5-flash"),
        gemini_rpm_limit=env_int("GEMINI_RPM_LIMIT", 10, minimum=1),
        gemini_rpd_limit=env_int("GEMINI_RPD_LIMIT", 250, minimum=1),
        gemini_tpd_limit=env_int("GEMINI_TPD_LIMIT", 250_000, minimum=1),
        openrouter_api_key=env_str("OPENROUTER_API_KEY"),
        openrouter_model=env_str(
            "OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free"
        ),
        openrouter_rpm_limit=env_int("OPENROUTER_RPM_LIMIT", 20, minimum=1),
        openrouter_rpd_limit=env_int("OPENROUTER_RPD_LIMIT", 50, minimum=1),
        openrouter_rpd_limit_upgraded=env_int(
            "OPENROUTER_RPD_LIMIT_UPGRADED", 1000, minimum=1
        ),
        ollama_base_url=env_str("OLLAMA_BASE_URL", "http://ollama:11434"),
        ollama_model=env_str("OLLAMA_MODEL", "llama3.2:3b"),
        ollama_enabled=env_bool("OLLAMA_ENABLED", False),
        llm_provider_order=env_list(
            "LLM_PROVIDER_ORDER", ["groq", "gemini", "openrouter", "ollama", "local"]
        ),
        llm_failover_threshold=env_float("LLM_FAILOVER_THRESHOLD", 0.85),
        langfuse_enabled=env_bool("LANGFUSE_ENABLED", False),
        langfuse_public_key=env_str("LANGFUSE_PUBLIC_KEY"),
        langfuse_secret_key=env_str("LANGFUSE_SECRET_KEY"),
        langfuse_host=env_str("LANGFUSE_HOST", "http://langfuse:3000"),
        telegram_bot_token=env_str("TELEGRAM_BOT_TOKEN"),
        telegram_allowed_chat_ids=env_list("TELEGRAM_ALLOWED_CHAT_IDS", []),
        telegram_polling_enabled=env_bool("TELEGRAM_POLLING_ENABLED", False),
        llm_guard_enabled=env_bool("LLM_GUARD_ENABLED", True),
        llm_guard_fail_closed=env_bool("LLM_GUARD_FAIL_CLOSED", True),
        prompt_injection_block_score=env_float(
            "PROMPT_INJECTION_BLOCK_SCORE", 0.85, minimum=0.0
        ),
        max_untrusted_content_chars=env_int(
            "MAX_UNTRUSTED_CONTENT_CHARS", 12_000, minimum=500
        ),
        crawl4ai_enabled=env_bool("CRAWL4AI_ENABLED", False),
        gpt_researcher_enabled=env_bool("GPT_RESEARCHER_ENABLED", False),
        scout_sources=env_list(
            "SCOUT_SOURCES",
            [
                "github_trending",
                "reddit_sideproject",
                "reddit_entrepreneur",
                "reddit_passiveincome",
                "gumroad",
                "appsumo",
                "youtube_trending",
                "tiktok_creative",
            ],
        ),
        scout_interval_minutes=env_int("SCOUT_INTERVAL_MINUTES", 360, minimum=30),
        scout_user_agent=env_str("SCOUT_USER_AGENT", "AutomatonHoldingBot/1.0 (+research)"),
        smoke_min_clicks=env_int("SMOKE_MIN_CLICKS", 25, minimum=1),
        smoke_min_conversion_rate=env_float(
            "SMOKE_MIN_CONVERSION_RATE", 0.04, minimum=0.0
        ),
        smoke_min_days=env_int("SMOKE_MIN_DAYS", 3, minimum=1),
        smoke_max_days=env_int("SMOKE_MAX_DAYS", 7, minimum=1),
        eval_min_score=env_float("EVAL_MIN_SCORE", 85.0, minimum=0.0),
        eval_max_retries=env_int("EVAL_MAX_RETRIES", 3, minimum=1),
        pexels_api_key=env_str("PEXELS_API_KEY"),
        pixabay_api_key=env_str("PIXABAY_API_KEY"),
        ffmpeg_binary=env_str("FFMPEG_BINARY", "ffmpeg"),
        video_output_dir=video_dir,
        video_target_width=env_int("VIDEO_TARGET_WIDTH", 1080, minimum=16),
        video_target_height=env_int("VIDEO_TARGET_HEIGHT", 1920, minimum=16),
        video_target_fps=env_int("VIDEO_TARGET_FPS", 30, minimum=1),
        video_max_duration_seconds=env_int(
            "VIDEO_MAX_DURATION_SECONDS", 58, minimum=5
        ),
        tts_provider=env_str("TTS_PROVIDER", "edge_tts"),
        tts_voice_pt=env_str("TTS_VOICE_PT", "pt-PT-RaquelNeural"),
        tts_voice_en=env_str("TTS_VOICE_EN", "en-US-AriaNeural"),
        moneyprinter_base_url=env_str("MONEYPRINTER_BASE_URL", "http://moneyprinter:8080"),
        shortgpt_enabled=env_bool("SHORTGPT_ENABLED", False),
        youtube_shorts_generator_enabled=env_bool(
            "YOUTUBE_SHORTS_GENERATOR_ENABLED", False
        ),
        genai_video_enabled=env_bool("GENAI_VIDEO_ENABLED", False),
        genai_video_order=env_list(
            "GENAI_VIDEO_ORDER", ["wan22", "longcat", "kandinsky", "ltx", "sdxl"]
        ),
        genai_timeout_seconds=env_int("GENAI_TIMEOUT_SECONDS", 600, minimum=30),
        wan22_endpoint=env_str("WAN22_ENDPOINT"),
        longcat_endpoint=env_str("LONGCAT_ENDPOINT"),
        kandinsky_endpoint=env_str("KANDINSKY_ENDPOINT"),
        ltx_endpoint=env_str("LTX_ENDPOINT"),
        sdxl_endpoint=env_str("SDXL_ENDPOINT"),
        research_max_results=env_int("RESEARCH_MAX_RESULTS", 5, minimum=1),
        research_timeout_seconds=env_int("RESEARCH_TIMEOUT_SECONDS", 240, minimum=30),
        gumroad_access_token=env_str("GUMROAD_ACCESS_TOKEN"),
        gumroad_email=env_str("GUMROAD_EMAIL"),
        lemonsqueezy_api_key=env_str("LEMONSQUEEZY_API_KEY"),
        lemonsqueezy_store_id=env_str("LEMONSQUEEZY_STORE_ID"),
        amazon_associate_tag=env_str("AMAZON_ASSOCIATE_TAG"),
        hotmart_affiliate_id=env_str("HOTMART_AFFILIATE_ID"),
        shopee_affiliate_id=env_str("SHOPEE_AFFILIATE_ID"),
        adsense_client_id=env_str("ADSENSE_CLIENT_ID"),
        wordpress_base_url=env_str("WORDPRESS_BASE_URL"),
        wordpress_app_password=env_str("WORDPRESS_APP_PASSWORD"),
        github_token=env_str("GITHUB_TOKEN"),
        github_pages_base_url=env_str(
            "GITHUB_PAGES_BASE_URL", "https://automaton-holding.github.io"
        ),
        tax_reserve_rate=env_float("TAX_RESERVE_RATE", 0.05, minimum=0.0),
        mpesa_base_url=env_str("MPESA_BASE_URL", "https://api.sandbox.vm.co.mz"),
        mpesa_api_key=env_str("MPESA_API_KEY"),
        mpesa_public_key=env_str("MPESA_PUBLIC_KEY"),
        mpesa_service_provider_code=env_str("MPESA_SERVICE_PROVIDER_CODE"),
        mpesa_origin=env_str("MPESA_ORIGIN", "+258840000000"),
        mpesa_initiator_identifier=env_str("MPESA_INITIATOR_IDENTIFIER"),
        mpesa_initiator_password=env_str("MPESA_INITIATOR_PASSWORD"),
        mpesa_security_credential=env_str("MPESA_SECURITY_CREDENTIAL"),
        mpesa_receiver_msisdn=env_str("MPESA_RECEIVER_MSISDN", "+258840000000"),
        mpesa_sandbox=env_bool("MPESA_SANDBOX", True),
        emola_base_url=env_str("EMOLA_BASE_URL", "https://api.emola.co.mz"),
        emola_api_key=env_str("EMOLA_API_KEY"),
        emola_merchant_code=env_str("EMOLA_MERCHANT_CODE"),
        emola_receiver_msisdn=env_str("EMOLA_RECEIVER_MSISDN", "+258860000000"),
        paypal_client_id=env_str("PAYPAL_CLIENT_ID"),
        paypal_client_secret=env_str("PAYPAL_CLIENT_SECRET"),
        paypal_mode=env_str("PAYPAL_MODE", "sandbox"),
        wise_api_token=env_str("WISE_API_TOKEN"),
        wise_profile_id=env_str("WISE_PROFILE_ID"),
        wise_default_target_currency=env_str("WISE_DEFAULT_TARGET_CURRENCY", "MZN"),
        hr_min_success_rate=env_float("HR_MIN_SUCCESS_RATE", 0.60, minimum=0.0),
        hr_max_cost_per_success_usd=env_float(
            "HR_MAX_COST_PER_SUCCESS_USD", 0.0, minimum=0.0
        ),
        judge_min_score=env_float("JUDGE_MIN_SCORE", 85.0, minimum=0.0),
    )


settings: Settings = _build_settings()


def get_settings() -> Settings:
    """Acessor para injecção em testes/routers."""
    return settings


__all__ = [
    "Settings",
    "settings",
    "get_settings",
    "REPO_ROOT",
    "env_str",
    "env_bool",
    "env_int",
    "env_float",
    "env_list",
]
