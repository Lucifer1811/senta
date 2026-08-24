"""
src/inference/llm.py — LiteLLM router wrapper for Phase 3 inference.

Model tiers (set in .env):
  replay  → SENTA_MODEL_REPLAY   (default: gemini/gemini-3.1-flash-lite-preview)
  strong  → SENTA_MODEL_STRONG   (default: gemini/gemini-3.1-flash-lite-preview)

Override either tier without code changes:
  SENTA_MODEL_REPLAY=gemini/gemini-flash-latest
  SENTA_MODEL_STRONG=gemini/gemini-flash-latest

Credentials read from .env via config.settings (never hardcoded here):
  GOOGLE_API_KEY (for all Gemini tiers)

infer() signature:
    (evidence_pack, valid_evidence_ids, tier, max_tokens, prompt_version) -> (dict, dict)

Returns:
    llm_out  — validated LLMOutput dict (keys match LLMOutput fields)
    usage    — {"model": str, "tokens_in": int, "tokens_out": int}

Raises:
    InvalidCitationError  — phantom citation in key_drivers
    ValidationError       — malformed JSON or schema violation (after retries)
    RuntimeError          — LiteLLM API error
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

_DEFAULT_MODELS: dict[str, str] = {
    "replay": "gemini/gemini-3.1-flash-lite-preview",
    "strong": "",  # must be set via SENTA_MODEL_STRONG in .env or shell
}

_DEFAULT_MAX_TOKENS: dict[str, int] = {
    "replay": 768,   # capped: median response is ~350 tok; 768 is headroom without waste
    "strong": 2048,
}

_MAX_RETRIES = 2   # retries on JSON parse / schema validation failure


def _get_model(tier: str) -> str:
    from config.settings import settings

    env_key   = f"SENTA_MODEL_{tier.upper()}"
    # Priority: shell env > settings (.env) > hardcoded default
    override  = os.environ.get(env_key) or getattr(settings, f"senta_model_{tier}", "")
    default   = _DEFAULT_MODELS.get(tier, "")

    if override:
        if override != default:
            logger.warning(
                "Model override: %s=%s (pinned default: %s) — "
                "remove %s from .env to revert",
                env_key, override, default or "(none)", env_key,
            )
        return override

    if not default:
        raise ValueError(
            f"No model configured for tier={tier!r}. "
            f"Set {env_key} in .env, e.g.:\n  {env_key}=anthropic/claude-sonnet-4-6"
        )
    return default


def _set_api_keys() -> None:
    """Propagate credentials from settings into os.environ for LiteLLM."""
    from config.settings import settings

    if settings.anthropic_api_key:
        os.environ.setdefault("ANTHROPIC_API_KEY", settings.anthropic_api_key)
    if settings.openai_api_key:
        os.environ.setdefault("OPENAI_API_KEY", settings.openai_api_key)
    if settings.google_api_key:
        os.environ.setdefault("GOOGLE_API_KEY", settings.google_api_key)


def infer(
    evidence_pack:      str,
    valid_evidence_ids: set[str],
    tier:               str = "replay",
    max_tokens:         int | None = None,
    prompt_version:     str = "v6",
) -> tuple[dict, dict]:
    """
    Run LLM inference on the evidence pack.

    Parameters
    ----------
    evidence_pack       Markdown string from assemble_evidence_pack().
    valid_evidence_ids  Set of evidence_ids present in the pack; phantom citations are rejected.
    tier                "replay" (cheap) or "strong" (Sonnet-class for Step 9.5).
    max_tokens          Token budget for the completion.
    prompt_version      Prompt template version key.

    Returns
    -------
    (llm_out_dict, usage_dict)
      llm_out_dict  keys: label, confidence, empirical_prior_used, key_drivers,
                         divergence_flags, abstain, rationale
      usage_dict    keys: model, tokens_in, tokens_out
    """
    import litellm
    from src.inference.prompts import get_system_prompt
    from src.inference.schemas import InvalidCitationError, LLMOutput

    _set_api_keys()

    model              = _get_model(tier)
    effective_max_tok  = max_tokens if max_tokens is not None else _DEFAULT_MAX_TOKENS.get(tier, 768)
    system_prompt      = get_system_prompt(prompt_version)

    _base_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": evidence_pack},
    ]
    messages = list(_base_messages)

    last_exc: Exception | None = None
    raw_text = ""
    for attempt in range(1, _MAX_RETRIES + 2):
        try:
            call_kwargs: dict = {
                "model":           model,
                "messages":        messages,
                "response_format": {"type": "json_object"},
                "max_tokens":      effective_max_tok,
            }
            # Gemini 3+ deprecates temperature; all other providers use it
            if not model.startswith("gemini/"):
                call_kwargs["temperature"] = 0.2
            resp = litellm.completion(**call_kwargs)
            raw_text  = resp.choices[0].message.content or ""
            usage_in  = getattr(resp.usage, "prompt_tokens",     0)
            usage_out = getattr(resp.usage, "completion_tokens", 0)

            # Extract JSON from response: strip markdown fences, then find outermost {...}
            stripped = raw_text.strip()
            if stripped.startswith("```"):
                parts = stripped.split("```", 2)
                inner = parts[1] if len(parts) >= 2 else stripped
                if inner.startswith("json"):
                    inner = inner[4:]
                stripped = inner.rsplit("```", 1)[0].strip()
            # Find outermost JSON object — handles preamble or thinking tokens before {
            start = stripped.find("{")
            end   = stripped.rfind("}")
            if start != -1 and end != -1 and end > start:
                stripped = stripped[start : end + 1]
            raw_text = stripped

            raw_dict = json.loads(raw_text)
            llm_out  = LLMOutput.model_validate(raw_dict)
            llm_out.validate_citations(valid_evidence_ids)

            logger.debug(
                "infer: model=%s tier=%s  label=%s  conf=%.2f  abstain=%s  in=%d  out=%d",
                model, tier, llm_out.label, llm_out.confidence, llm_out.abstain,
                usage_in, usage_out,
            )
            return llm_out.model_dump(), {"model": model, "tokens_in": usage_in, "tokens_out": usage_out}

        except json.JSONDecodeError as exc:
            logger.warning("infer attempt %d/%d JSON parse error: %s", attempt, _MAX_RETRIES + 1, exc)
            last_exc = exc
            if attempt <= _MAX_RETRIES:
                messages = [
                    _base_messages[0],
                    _base_messages[1],
                    {"role": "assistant", "content": raw_text or ""},
                    {"role": "user", "content": (
                        "Your previous response was not valid JSON. "
                        "Return ONLY the JSON object — no prose, no markdown fences, no explanation."
                    )},
                ]
                continue
            break

        except InvalidCitationError as exc:
            logger.warning("infer attempt %d/%d citation error: %s", attempt, _MAX_RETRIES + 1, exc)
            last_exc = exc
            if attempt <= _MAX_RETRIES:
                valid_ids_str = "\n".join(sorted(valid_evidence_ids))
                messages = [
                    _base_messages[0],
                    _base_messages[1],
                    {"role": "assistant", "content": raw_text or ""},
                    {"role": "user", "content": (
                        f"Your response cited an evidence_id that does not exist in the pack.\n"
                        f"The ONLY valid evidence_ids are:\n{valid_ids_str}\n\n"
                        "Return the corrected JSON object only — use exclusively IDs from the list above."
                    )},
                ]
                continue
            break

        except Exception as exc:
            import time as _time
            err_str = str(exc)
            is_rate_limit = "429" in err_str or "rate" in err_str.lower() or "quota" in err_str.lower()
            is_transient  = is_rate_limit or "503" in err_str or "unavailable" in err_str.lower() or "overload" in err_str.lower()
            if is_transient and attempt <= _MAX_RETRIES:
                wait = 60.0 * attempt if is_rate_limit else 10.0 * attempt
                logger.warning(
                    "infer attempt %d/%d transient error (rate=%s); waiting %.0fs — %s",
                    attempt, _MAX_RETRIES + 1, is_rate_limit, wait, str(exc)[:80],
                )
                _time.sleep(wait)
                continue
            if is_transient:
                logger.error("infer attempt %d/%d transient error exhausted retries: %s", attempt, _MAX_RETRIES + 1, exc)
                raise RuntimeError(f"LiteLLM API error on tier={tier} model={model}") from exc
            # Schema / validation error — not retryable
            logger.warning("infer attempt %d/%d schema error: %s", attempt, _MAX_RETRIES + 1, exc)
            last_exc = exc
            break

    raise last_exc or RuntimeError("infer: exhausted retries without a valid response")
