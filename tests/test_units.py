"""Small pure functions: link cleaning, cost maths, quality thresholds, prompt filling."""
from agent import config, costs, quality, resources, store


def test_tracking_parameters_are_stripped():
    url = "https://learn.example.in/l/iim-ai/?utm_source=Webinar&utm_medium=Promotional&id=7"
    assert resources._clean(url) == "https://learn.example.in/l/iim-ai/?id=7"
    assert resources._clean("https://www.youtube.com/watch?v=abcdefghijk&si=xyz") == \
        "https://www.youtube.com/watch?v=abcdefghijk"


def test_unverified_links_are_dropped(monkeypatch):
    monkeypatch.setattr(resources, "_youtube_exists", lambda url: False)
    sources = {resources._norm("https://www.gartner.com/en/articles/x")}
    assert resources._verified_url("https://gartner.com/en/articles/x/", sources)
    assert resources._verified_url("https://made-up.example.com/course", sources) == ""
    assert resources._verified_url("not a url", sources) == ""


def test_live_usage_is_summed_per_turn():
    u = costs.Usage()
    for _ in range(2):
        u.add_live({"promptTokensDetails": [{"modality": "TEXT", "tokenCount": 1000},
                                            {"modality": "AUDIO", "tokenCount": 500}],
                    "responseTokensDetails": [{"modality": "AUDIO", "tokenCount": 200}],
                    "thoughtsTokenCount": 50})
    assert (u.text_in, u.audio_in, u.audio_out, u.text_out) == (2000, 1000, 400, 100)
    expected_usd = (2000 * config.PRICE_LIVE_TEXT_IN + 1000 * config.PRICE_LIVE_AUDIO_IN
                    + 100 * config.PRICE_LIVE_TEXT_OUT + 400 * config.PRICE_LIVE_AUDIO_OUT) / 1e6
    assert abs(u.live_inr() - expected_usd * config.USD_INR) < 1e-9


def test_vobiz_estimate_rounds_up_to_minutes():
    assert costs.vobiz_estimate_inr(143) == 3 * config.VOBIZ_RATE_PER_MIN_INR
    assert costs.vobiz_estimate_inr(5) == config.VOBIZ_RATE_PER_MIN_INR


def test_quality_failures_respect_thresholds():
    result = {"scores": {k: 5 for k in quality.RUBRIC}}
    result["scores"]["english_only"] = 3
    result["scores"]["closing"] = None  # not applicable
    assert quality.failures(result) == ["english_only=3 (min 4.5)"]


def test_prompt_placeholders_are_filled():
    p = config.system_prompt("Alok", "resume {with braces}", "(none)", "alok@example.com", True)
    assert "{name}" not in p and "{delivery}" not in p and "{history}" not in p
    assert "resume {with braces}" in p and "a…@example.com" in p and "WhatsApp" in p


def test_history_text_includes_unsaved_calls():
    text = store.history_text({"calls": [{"date": "d", "duration": 40, "ended": "dropped",
                                          "summary": {}, "transcript": "Coach: hi"}]})
    assert "CALL 1" in text and "No summary was saved" in text and "Coach: hi" in text
