"""Environment-driven settings.

Loads `.env` via pydantic-settings. Exposes a `settings` singleton. Env var
names match `.env.example` EXACTLY. Provides `is_testnet` plus the testnet /
mainnet REST + WebSocket base URLs from the Binance integration guide.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Binance USDⓈ-M Futures hosts (see docs/integrations/binance-futures-api.md).
TESTNET_REST_URL = "https://testnet.binancefuture.com"
MAINNET_REST_URL = "https://fapi.binance.com"
TESTNET_WS_URL = "wss://stream.binancefuture.com"
MAINNET_WS_URL = "wss://fstream.binance.com"


class Settings(BaseSettings):
    """Typed application settings sourced from the environment / `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Environment selection ---
    trading_env: str = Field(default="testnet")  # testnet | mainnet
    binance_testnet_key: str = Field(default="")
    binance_testnet_secret: str = Field(default="")
    binance_mainnet_key: str = Field(default="")
    binance_mainnet_secret: str = Field(default="")
    # Explicit live-trading confirmation. MAINNET refuses to connect unless this
    # is exactly "yes" — prevents a TRADING_ENV typo from trading real money.
    mainnet_confirmed: str = Field(default="")

    # --- Global risk caps (enforced by the risk engine; bots cannot exceed) ---
    max_leverage: int = Field(default=5)
    risk_pct_per_trade: float = Field(default=1.0)
    max_daily_loss_pct: float = Field(default=4.0)
    max_account_drawdown_pct: float = Field(default=20.0)
    max_concurrent_positions: int = Field(default=5)
    max_notional_per_symbol_pct: float = Field(default=30.0)
    default_margin_type: str = Field(default="ISOLATED")
    min_signal_confidence: float = Field(default=0.55)

    # --- Trading costs (Binance USDⓈ-M defaults; % of notional, PER SIDE) ---
    # Taker = market fill, maker = resting-limit fill. Slippage is an estimated
    # adverse price impact applied to MARKET (taker) legs only. These feed every
    # PnL calc so reported P&L and the ML win/loss labels are NET, not gross.
    taker_fee_pct: float = Field(default=0.04)
    maker_fee_pct: float = Field(default=0.02)
    slippage_pct: float = Field(default=0.02)

    # --- Optional signal engines ---
    tv_webhook_secret: str = Field(default="")
    ollama_base_url: str = Field(default="http://localhost:11434")
    ollama_model: str = Field(default="llama3.1")

    # --- Ops ---
    db_path: str = Field(default="./data/trader.db")
    log_level: str = Field(default="INFO")
    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8000)

    @field_validator("trading_env")
    @classmethod
    def _validate_env(cls, v: str) -> str:
        """Reject typos that would silently flip testnet/mainnet selection."""
        normalized = str(v).strip().lower()
        if normalized not in ("testnet", "mainnet"):
            raise ValueError(
                f"TRADING_ENV must be 'testnet' or 'mainnet', got {v!r}"
            )
        return normalized

    # --- Derived helpers ---
    @property
    def is_testnet(self) -> bool:
        """True unless TRADING_ENV is explicitly `mainnet`."""
        return self.trading_env.strip().lower() != "mainnet"

    @property
    def api_key(self) -> str:
        """Active API key for the selected environment."""
        return self.binance_testnet_key if self.is_testnet else self.binance_mainnet_key

    @property
    def api_secret(self) -> str:
        """Active API secret for the selected environment."""
        return (
            self.binance_testnet_secret if self.is_testnet else self.binance_mainnet_secret
        )

    @property
    def has_credentials(self) -> bool:
        """True when both key and secret are present for the active environment."""
        return bool(self.api_key.strip()) and bool(self.api_secret.strip())

    @property
    def rest_base_url(self) -> str:
        """REST base host for the selected environment."""
        return TESTNET_REST_URL if self.is_testnet else MAINNET_REST_URL

    @property
    def ws_base_url(self) -> str:
        """WebSocket market-stream base for the selected environment."""
        return TESTNET_WS_URL if self.is_testnet else MAINNET_WS_URL

    @property
    def env_label(self) -> str:
        """Canonical environment label for the API (`testnet` | `mainnet`)."""
        return "testnet" if self.is_testnet else "mainnet"

    def risk_caps(self) -> dict:
        """Risk caps payload matching the GET /api/config contract shape."""
        return {
            "max_leverage": self.max_leverage,
            "risk_pct_per_trade": self.risk_pct_per_trade,
            "max_daily_loss_pct": self.max_daily_loss_pct,
            "max_account_drawdown_pct": self.max_account_drawdown_pct,
            "max_concurrent_positions": self.max_concurrent_positions,
            "max_notional_per_symbol_pct": self.max_notional_per_symbol_pct,
            "default_margin_type": self.default_margin_type,
            "min_signal_confidence": self.min_signal_confidence,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton (cached)."""
    return Settings()


# Module-level singleton for convenient import: `from app.config import settings`.
settings = get_settings()
