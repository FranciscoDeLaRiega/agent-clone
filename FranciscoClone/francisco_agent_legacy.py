from a2a.server.agent_execution import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.tasks.task_updater import TaskUpdater
from a2a.types import TextPart, TaskState

from openai import AsyncOpenAI
from typing import Any

from a2a.utils import get_message_text
import re
from loguru import logger
from .memory_management import MemoryStore
from .browser import BrowserUseClient

_URL_REGEXES = [
    re.compile(r"https?://[^\s]+", re.IGNORECASE),
    re.compile(r"\bwww\.[^\s/]+\.[^\s]+", re.IGNORECASE),
    re.compile(
        r"\b[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
        r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
        r"(?:/[^\s]*)?\b",
        re.IGNORECASE,
    ),
]

_KEYWORD_REGEXES = [
    r"\bgo to\b",
    r"\bvisit\b",
    r"\bopen (?:site|page|link)\b",
    r"\bclick\b",
    r"\bnavigate\b",
    r"\bfill\b",
    r"\bsubmit\b",
    r"\blog ?in\b",
    r"\bsign ?up\b",
    r"\bplay\b",
    r"\btic-?tac-?toe\b",
    r"\badd to cart\b",
    r"\bcheckout\b",
    r"\bscrape\b",
    r"\bdownload\b",
    r"\bupload\b",
    r"\bgoogle docs\b",
    r"\bsalesforce\b",
    r"\blinked?in\b",
    r"\bbrowser\b",
]
_KEYWORD_REGEXES = [re.compile(pat, re.IGNORECASE) for pat in _KEYWORD_REGEXES]

_MEMORY_NOTE_RE = re.compile(r"(?is)\bmemory\s*note\s*:\s*(.+)$")


class CloneExecutor(AgentExecutor):
    """
    Executor for Francisco's Clone.

    A multi-skill agent that can:
    1. Solve basic math problems
    2. Perform SHA-512/MD5 hash pipelines
    3. Analyze and describe images
    4. Browse the web (e.g. win Tic-tac-toe)
    5. Generate and run code (e.g. brute-force algorithms)
    6. Remember context across sessions
    """
    def __init__(self, api_key: str):
        self.system_prompt = (
            "Francisco's Clone: a multi-skill AI agent with six abilities:\n"
            "1) Math: solve basic problems step-by-step.\n"
            "2) Hashing (SHA-512, MD5): perform chained hashing operations and return the final digest.\n"
            "3) Vision: analyze images and answer questions.\n"
            "4) Web: browse or interact when possible, otherwise explain limits.\n"
            "5) Code: generate and run algorithms; show results.\n"
            "6) Memory: recall key facts and add a short 'Memory note' after answers.\n"
        )
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = "gpt-5"
        self.memory = MemoryStore()
        self.browser = BrowserUseClient(api_key)

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Main entrypoint: process input, run model/browser, send result."""
        logger.info("Start: task_id=%s ctx=%s model=%s", context.task_id, context.context_id, self.model)

        user_text = context.get_user_input()
        logger.debug("Input length=%d", len(user_text or ""))
        if not user_text:
            logger.warning("Empty input → sending error")
            await self._fail_task(context, event_queue, "No input provided.")
            return

        task_updater = TaskUpdater(event_queue=event_queue, task_id=context.task_id, context_id=context.context_id)
        await task_updater.start_work()

        #build memory/context
        logger.debug("Hash request? %s", self._looks_like_hash_task(user_text))
        memory_prefix = self._build_history_prefix(context)
        user_key = MemoryStore.get_user_key(getattr(context, "metadata", {}))
        long_term = self.memory.summary(user_key)
        if long_term:
            memory_prefix = (memory_prefix + ("\n\n" if memory_prefix else "")) + "Long-term memory:\n" + long_term
        logger.debug("Memory prefix length=%d", len(memory_prefix or ""))

        input_payload = self._build_input_payload(context, memory_prefix, user_text)
        logger.debug("Built model input payload")

        #Try browser-use first if it looks like a browser task
        answer_text: str | None = None
        if self._looks_like_browser_task(user_text):
            logger.info("Detected browser task → using browser-use")
            try:
                answer_text = await self.browser.run_task(user_text)
                logger.info("Browser-use success (chars=%d)", len(answer_text or ""))
            except Exception as e:
                logger.warning("Browser-use failed → fallback. Err=%s", str(e))

        errors: list[str] = []

        async def _try_responses(tools: list[dict[str, Any]] | None) -> tuple[str | None, str | None]:
            """Call Responses API, extract text or return error string; preserves original extraction logic."""
            try:
                if tools is None:
                    logger.info("Responses API (no-tools)")
                    resp = await self.client.responses.create(model=self.model, input=input_payload)
                else:
                    logger.info("Responses API (tools=%s)", tools)
                    resp = await self.client.responses.create(
                        model=self.model, input=input_payload, tools=tools, tool_choice="auto"
                    )
                text = getattr(resp, "output_text", None)
                if not text:
                    resp_dict = resp.model_dump() if hasattr(resp, "model_dump") else resp.__dict__
                    text = self._get_text_from_response(resp_dict)
                logger.info("Responses API%s returned=%s",
                            "" if tools is None else " (tools)", bool(text))
                return text, None
            except Exception as e:
                msg = str(e)
                logger.warning("Responses API%s error: %s",
                            "" if tools is None else " (tools)", msg)
                return None, msg

        if not answer_text:
            #with tools
            tools_variants = [[{"type": "code_interpreter", "container": {"type": "auto"}}]]
            for tools in tools_variants:
                text, err = await _try_responses(tools)
                if text:
                    answer_text = text
                    break
                if err:
                    errors.append(err)

        if not answer_text:
            #no-tools
            text, err = await _try_responses(None)
            if text:
                answer_text = text
            elif err:
                errors.append(err)

        if not answer_text:
            #fallback
            logger.info("Fallback → chat.completions")
            try:
                messages = [
                    {"role": "system", "content": self.system_prompt + ("\n\n" + memory_prefix if memory_prefix else "")},
                    {"role": "user", "content": self._add_hash_rules_if_needed(user_text)},
                ]
                comp = await self.client.chat.completions.create(
                    model=self.model, messages=messages, max_tokens=1200, temperature=0.2
                )
                answer_text = comp.choices[0].message.content
                logger.info("Chat completions returned=%s", bool(answer_text))
            except Exception as e:
                logger.error("All attempts failed: %s | last=%s", errors, str(e))
                await self._fail_task(context, event_queue, f"OpenAI request failed: {' | '.join(errors + [str(e)])}")
                return

        #emit artifacts & complete
        await task_updater.add_artifact(parts=[TextPart(text=answer_text or "")], name="agent_output", last_chunk=True)
        logger.info("Added artifact (len=%d)", len(answer_text or ""))

        answer_msg = task_updater.new_agent_message([TextPart(text=answer_text or "")])
        try:
            await task_updater.update_status(TaskState.working, message=answer_msg, final=False)
            logger.debug("Interim answer pushed to history")
        except Exception as e:
            logger.debug("Failed to push interim answer: %s", str(e))

        self._save_memory_note(user_key, answer_text or "")
        await task_updater.complete(answer_msg)
        logger.info("Task complete: task_id=%s", context.task_id)


    def _looks_like_hash_task(self, text: str) -> bool:
        """Check if text mentions md5 or sha512."""
        return bool(re.search(r"\b(md5|sha512)\b", (text or "")))


    def _add_hash_rules_if_needed(self, user_text: str) -> str:
        """Append hashing rules if input is a hash request."""
        if not self._looks_like_hash_task(user_text):
            return user_text
        logger.debug("Adding hash directive")
        return user_text + "\n\n[Hashing] Use lowercase hex chain. Return final digest only."


    def _build_history_prefix(self, context: RequestContext) -> str:
        """Build conversation history summary for model context."""
        history = []
        count = 0
        try:
            task = context.current_task
            if task and task.history:
                for m in task.history[-6:]:
                    role = m.role.value if hasattr(m.role, "value") else str(m.role)
                    text = get_message_text(m)
                    if text:
                        history.append(f"{role}: {text}")
                        count += 1
        except Exception as e:
            logger.debug("Memory build skipped (err=%s)", str(e))
        if not history:
            return ""
        joined = "\n".join(history)[-2000:]
        logger.debug("Memory from %d messages", count)
        return "Prior conversation:\n" + joined


    def _build_input_payload(self, context: RequestContext, memory_prefix: str, user_text: str) -> list[dict[str, Any]]:
        """Prepare input payload for OpenAI Responses API."""
        system_block = {
            "role": "system",
            "content": [{"type": "input_text", "text": self.system_prompt + ("\n\n" + memory_prefix if memory_prefix else "")}],
        }
        user_text_effective = self._add_hash_rules_if_needed(user_text)
        user_content = [{"type": "input_text", "text": user_text_effective}]
        image_count = 0
        try:
            if context.message and context.message.parts:
                for p in context.message.parts:
                    part = getattr(p, "root", p)
                    kind = getattr(part, "kind", None) or getattr(part, "type", None)
                    if kind == "file":
                        file_obj = getattr(part, "file", None)
                        if not file_obj:
                            continue
                        b64 = getattr(file_obj, "bytes", None)
                        mime = getattr(file_obj, "mime_type", None) or getattr(file_obj, "mimeType", None)
                        uri = getattr(file_obj, "uri", None) or getattr(file_obj, "url", None)
                        if not uri and hasattr(file_obj, "model_dump"):
                            d = file_obj.model_dump()
                            uri = d.get("uri") or d.get("url")
                            mime = mime or d.get("mime_type") or d.get("mimeType")
                            b64 = b64 or d.get("bytes")
                        if b64 and mime and mime.startswith("image/"):
                            user_content.append({"type": "input_image", "image_url": f"data:{mime};base64,{b64}"})
                            image_count += 1
                            continue
                        if uri and mime and mime.startswith("image/"):
                            user_content.append({"type": "input_image", "image_url": uri})
                            image_count += 1
        except Exception as e:
            logger.debug("Image parse skipped (err=%s)", str(e))
        logger.debug("Prepared payload: images=%d", image_count)
        return [system_block, {"role": "user", "content": user_content}]


    async def _fail_task(self, context: RequestContext, event_queue: EventQueue, error_message: str):
        """Send error message and mark task as failed."""
        logger.error("Error: %s", error_message)
        task_updater = TaskUpdater(event_queue=event_queue, task_id=context.task_id, context_id=context.context_id)
        await task_updater.failed(task_updater.new_agent_message([TextPart(text=error_message)]))


    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Cancel running task and notify user."""
        logger.info("Cancel: task_id=%s", context.task_id)
        task_updater = TaskUpdater(event_queue=event_queue, task_id=context.task_id, context_id=context.context_id)
        await task_updater.cancel(task_updater.new_agent_message([TextPart(text="Task cancelled by user")]))


    def _save_memory_note(self, user_key: str, text: str) -> None:
        """Find 'Memory note:' in model output and store it."""
        if not text:
            return

        match = _MEMORY_NOTE_RE.search(text)
        if not match:
            return

        note = match.group(1).strip()[:2000]
        try:
            self.memory.append_note(user_key, note)
            logger.debug("Memory note saved for user=%s", user_key)
        except Exception as e:
            logger.debug("Failed to save memory note for user=%s: %s", user_key, e)

    def _get_text_from_response(self, resp: dict[str, Any]) -> str | None:
        """Extract plain text from a Responses API payload, handling multiple formats."""
        try:
            output = resp.get("output") or resp.get("response") or resp.get("data")
            if isinstance(output, list) and output:
                first = output[0]
                if isinstance(first, dict):
                    content = first.get("content")
                    if isinstance(content, list):
                        for c in content:
                            if c.get("type") in ("output_text", "text"):
                                t = c.get("text") or c.get("value")
                                return t.get("content") if isinstance(t, dict) else t

            if "choices" in resp:
                choice = resp["choices"][0]
                return choice.get("message", {}).get("content")

        except Exception as e:
            logger.debug("Response text extraction failed: %s", e)

        return None


    def _looks_like_browser_task(self, text: str) -> bool:
        """Check if text implies a browser task (keywords or URLs)."""
        if not text:
            return False

        if any(rx.search(text) for rx in _KEYWORD_REGEXES):
            return True

        if any(rx.search(text) for rx in _URL_REGEXES):
            return True

        return False
