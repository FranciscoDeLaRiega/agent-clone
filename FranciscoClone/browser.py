import asyncio
import os
from typing import Any, Optional
import re
import json
from loguru import logger
from browser_use import Agent as BrowserAgent
from browser_use import ChatOpenAI as BrowserChatOpenAI

_SECRET_14_RE = re.compile(r"\b(20\d{12})\b")

class BrowserUseClient:
    """
    Thin async wrapper around browser-use.
    - Uses OpenAI via browser_use.ChatOpenAI.
    - Runs an Agent with a timeout.
    - Normalizes outputs (str/dict/Pydantic) into a string.
    """

    def __init__(
        self,
        openai_api_key: Optional[str] = None,
        model: str = "gpt-5-mini",
        timeout_seconds: int = 300,
    ) -> None:
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._openai_api_key = openai_api_key or os.getenv("OPENAI_API_KEY") or ""
        if not self._openai_api_key:
            logger.warning("No OPENAI_API_KEY provided or found in environment.")

        self._llm = BrowserChatOpenAI(model=self.model, api_key=self._openai_api_key)

    async def run_task(self, task_text: str) -> str:
        """
        Run a browser-use Agent and return a robust, non-empty string.
        Retries transient failures and guarantees a non-empty payload.
        """
        agent = BrowserAgent(task=task_text, llm=self._llm)

        last_err = None
        for attempt in range(3):
            try:
                result: Any = await asyncio.wait_for(agent.run(), timeout=self.timeout_seconds)
                text = self._normalize_result(result)

                if not text or not text.strip() or text.strip() in {
                    "(browser-use finished with no output)",
                    "(browser-use: empty output)",
                }:
                    raise RuntimeError("empty browser-use output")

                if "502" in text or "Bad Gateway" in text:
                    raise RuntimeError("transient 502 from browser-use")

                return text
            except asyncio.TimeoutError as e:
                last_err = e
                logger.warning("browser-use timeout (attempt {}): {}", attempt + 1, e)
            except asyncio.CancelledError:
                logger.debug("browser-use run was cancelled")
                raise
            except Exception as e:
                last_err = e
                logger.warning("browser-use transient error (attempt {}): {}", attempt + 1, e)

            await asyncio.sleep(0.5 * (attempt + 1))

        return f'(browser-use error after retries: {last_err})'

    def _normalize_result(self, result: Any) -> str:
        """Return a non-empty, JSON-safe string."""
        try:
            if result is None:
                return "(browser-use finished with no output)"

            #Pydantic-like models
            if hasattr(result, "model_dump"):
                try:
                    dumped = result.model_dump()
                    text = _extract_text_from_browser_use_output(dumped)
                    return text if text and text.strip() else json.dumps(dumped, ensure_ascii=False)
                except Exception:
                    return "(browser-use: model_dump failed)"

            #Dict payloads
            if isinstance(result, dict):
                text = _extract_text_from_browser_use_output(result)
                return text if text and text.strip() else json.dumps(result, ensure_ascii=False)

            #Strings
            if isinstance(result, str):
                s = result.strip()
                return s if s else "(browser-use: empty output)"

            #Lists/others 
            s = str(result).strip()
            return s if s else "(browser-use: empty output)"
        except Exception as e:
            logger.debug("Failed to normalize browser-use result: {}", e)
            return "(browser-use: normalize error)"


def _extract_text_from_browser_use_output(payload: dict[str, Any]) -> Optional[str]:
    """
    Best-effort extraction of a human-readable summary from browser-use output.
    - Checks common summary keys.
    - Falls back to concatenating 'text' fields from a 'content' list.
    """
    for key in ("output_text", "final_result", "text", "summary"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            m = _SECRET_14_RE.search(val)
            return m.group(1) if m else val

    content = payload.get("content")
    if isinstance(content, list):
        texts = [c.get("text") for c in content if isinstance(c, dict) and isinstance(c.get("text"), str)]
        joined = "\n".join(t for t in texts if t)
        if joined.strip():
            m = _SECRET_14_RE.search(joined)
            return m.group(1) if m else joined

    #Last resort: scan the whole payload
    blob = json.dumps(payload, ensure_ascii=False)
    m = _SECRET_14_RE.search(blob)
    if m:
        return m.group(1)
    return None
