"""Cost per call in INR, from what each provider actually reports.

- Gemini Live: usageMetadata arrives once per turn. Each turn's prompt count
  includes the whole conversation so far (it is billed again every turn), so the
  per-turn figures are summed. Prices are list estimates set in config.
- Research, quality scoring: Gemini text usage plus Google Search queries.
- Vobiz: the billed cost from the call record.
"""
from agent import config


class Usage:
    """Running Gemini token totals by kind."""

    def __init__(self):
        self.text_in = self.audio_in = self.text_out = self.audio_out = 0
        self.flash_in = self.flash_out = self.searches = 0

    def add_live(self, um: dict) -> None:
        for d in um.get("promptTokensDetails", []):
            if d.get("modality") == "AUDIO":
                self.audio_in += d.get("tokenCount", 0)
            else:
                self.text_in += d.get("tokenCount", 0)
        for d in um.get("responseTokensDetails", []):
            if d.get("modality") == "AUDIO":
                self.audio_out += d.get("tokenCount", 0)
            else:
                self.text_out += d.get("tokenCount", 0)
        self.text_out += um.get("thoughtsTokenCount", 0)  # thinking is billed as text output

    def add_flash(self, um: dict, searches: int = 0) -> None:
        self.flash_in += um.get("promptTokenCount", 0)
        self.flash_out += um.get("candidatesTokenCount", 0) + um.get("thoughtsTokenCount", 0)
        self.searches += searches

    def live_inr(self) -> float:
        usd = (self.text_in * config.PRICE_LIVE_TEXT_IN + self.audio_in * config.PRICE_LIVE_AUDIO_IN
               + self.text_out * config.PRICE_LIVE_TEXT_OUT + self.audio_out * config.PRICE_LIVE_AUDIO_OUT) / 1e6
        return usd * config.USD_INR

    def flash_inr(self) -> float:
        usd = (self.flash_in * config.PRICE_FLASH_IN + self.flash_out * config.PRICE_FLASH_OUT) / 1e6
        return (usd + self.searches * config.PRICE_SEARCH_PER_QUERY) * config.USD_INR

    def tokens(self) -> dict:
        return {k: v for k, v in vars(self).items()}


def vobiz_estimate_inr(duration_secs: float) -> float:
    minutes = max(1, -(-int(duration_secs) // 60))  # billed per started minute
    return minutes * config.VOBIZ_RATE_PER_MIN_INR
