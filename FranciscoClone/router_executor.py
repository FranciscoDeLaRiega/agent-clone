from typing import Any
import re
from loguru import logger
import json
from a2a.server.agent_execution import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.tasks.task_updater import TaskUpdater
from a2a.types import TextPart, TaskState
from a2a.utils import get_message_text
import base64, mimetypes
from openai import AsyncOpenAI

from .browser import BrowserUseClient
from .memory_management import MemoryStore
from .router import route_request


class RoutingExecutor(AgentExecutor):
    def __init__(self, api_key: str):
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = "gpt-5"
        self.browser = BrowserUseClient(api_key, model="gpt-5", timeout_seconds=150)
        self.memory = MemoryStore()
        self.system_prompt = (
            "Francisco's Clone: multi-skill agent.\n"
            "Abilities: math, hashing (SHA-512/MD5), vision Q&A, web browsing, code gen/exec, long-term memory.\n"
            "If a task asks for a 14-digit secret from a page, return ONLY that 14-digit number."
        )

        #all regexes scoped to the class
        self._pure_14_re = re.compile(r"\b(\d{14})\b")
        self._digitish_14_re = re.compile(r"(?:\d[\d\-\s]{13,})")
        self._secret_intent_re = re.compile(r"\b(secret|14[- ]?digit|code|number|digits?)\b", re.I)
        self._pair_query_re = re.compile(r"\bpaired with\s+(\d{2,})\b", re.I)
        self._pair_rx = re.compile(r"(?<!\d)(\d{2,})\s*(?:↔|<->|->|=>|→|—|–|:|-|,|\||/|\\)\s*(\d{2,})(?!\d)")
        self._pair_fallback_rx = re.compile(r"(?<!\d)(\d{2,})\D{1,6}(\d{2,})(?!\d)")

    #public API

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        self._ctx = context
        task_updater = TaskUpdater(event_queue=event_queue, task_id=context.task_id, context_id=context.context_id)
        await task_updater.start_work()

        user_text = context.get_user_input()
        user_key = MemoryStore.get_user_key(getattr(context, "metadata", {}))

        if not user_text:
            await self._fail(task_updater, "No input provided.")
            return
    
        self.memory.ingest_text_for_pairs(user_key, user_text or "")
        self._save_memory_note(user_key, user_text or "")
        await self._ingest_pairs_from_history(context, user_key)
        
        direct = self._maybe_answer_number_pair_query(context, user_key, user_text)

        if direct is not None:
            answer = direct
            await task_updater.add_artifact(parts=[TextPart(text=answer)], name="agent_output", last_chunk=True)
            msg = task_updater.new_agent_message([TextPart(text=answer)])
            await task_updater.update_status(TaskState.working, message=msg, final=False)
            await task_updater.complete(msg)
            return
        
        memory_prefix = await self._history_prefix(context)
        long_term = self.memory.summary(user_key)
        if long_term:
            memory_prefix += ("\n\n" if memory_prefix else "") + "Long-term memory:\n" + long_term

        route = route_request(context)
        logger.info("Routing decision: %s", route)

        try:
            if route == "web":
                answer = await self._handle_web(user_text)
            else:
                answer = await self._handle_llm(context, memory_prefix, user_text)

        except Exception as e:
            await self._fail(task_updater, f"Execution error: {e}")
            return

        await task_updater.add_artifact(parts=[TextPart(text=answer or "")], name="agent_output", last_chunk=True)
        msg = task_updater.new_agent_message([TextPart(text=answer or "")])
        await task_updater.update_status(TaskState.working, message=msg, final=False)
        await task_updater.complete(msg)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        logger.info("Cancel: task_id=%s", context.task_id)
        task_updater = TaskUpdater(event_queue=event_queue, task_id=context.task_id, context_id=context.context_id)
        await task_updater.cancel(task_updater.new_agent_message([TextPart(text="Task cancelled by user")]))


    #internals

    async def _fail(self, task_updater: TaskUpdater, message: str) -> None:
        state = getattr(TaskState, "failed", None) or getattr(TaskState, "error", None) or TaskState.working
        await task_updater.add_artifact(parts=[TextPart(text=message)], name="agent_error", last_chunk=True)
        msg = task_updater.new_agent_message([TextPart(text=message)])
        try:
            await task_updater.update_status(state, message=msg, final=True)
        except Exception:
            pass
        await task_updater.complete(msg)

    async def _handle_web(self, user_text: str) -> str:
        try:
            out = await self.browser.run_task(user_text)
        except RuntimeError as e:
            out = f"(browser timed out: {e})"

        if isinstance(out, dict):
            try:
                out = json.dumps(out, ensure_ascii=False)
            except Exception:
                out = str(out)

        if self._secret_intent_re.search(user_text or ""):
            num = self._extract_plain_14_digits(out or "")
            if num:
                return num

        return out or ""

    async def _handle_llm(self, context, memory_prefix: str, user_text: str) -> str:
        images = self._iter_image_parts(context)

        vision_instr = []
        if images:
            vision_instr = [{
                "type": "input_text",
                "text": "Classify the main object in the image. Reply with a single lowercase label only."
            }]

        system = {
            "role": "system",
            "content": [{
                "type": "input_text",
                "text": self.system_prompt + (("\n\n" + memory_prefix) if memory_prefix else "")
            }],
        }
        user = {
            "role": "user",
            "content": vision_instr + [{"type": "input_text", "text": user_text}] + images,
        }

        try:
            resp = await self.client.responses.create(
                model=self.model,
                input=[system, user],
                tools=[{"type": "code_interpreter", "container": {"type": "auto"}}],
                tool_choice="auto",
            )
            text = getattr(resp, "output_text", None)
            if not text:
                blob = resp.model_dump() if hasattr(resp, "model_dump") else resp.__dict__
                text = self._extract_text(blob)
            if self._secret_intent_re.search(user_text or ""):
                num = self._extract_plain_14_digits(text or "")
                if num:
                    return num
            if text:
                return text
        except Exception as e:
            logger.warning("Responses API (tools) error: {}", e)

        #Fallback: Responses API without tools
        try:
            resp = await self.client.responses.create(model=self.model, input=[system, user])
            text = getattr(resp, "output_text", None)
            if not text:
                blob = resp.model_dump() if hasattr(resp, "model_dump") else resp.__dict__
                text = self._extract_text(blob)
            if self._secret_intent_re.search(user_text or ""):
                num = self._extract_plain_14_digits(text or "")
                if num:
                    return num
            if text:
                return text
        except Exception as e:
            logger.warning("Responses API (no-tools) error: {}", e)

        #Last resort: chat.completions 
        comp = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": self.system_prompt + (("\n\n" + memory_prefix) if memory_prefix else "")},
                {"role": "user", "content": user_text},
            ],
            max_tokens=1200,
            temperature=0.2,
        )
        text = comp.choices[0].message.content or ""
        if self._secret_intent_re.search(user_text or ""):
            num = self._extract_plain_14_digits(text or "")
            if num:
                return num
        if images:
            return "image_missing"
        return text

    
    async def _ingest_pairs_from_history(self, context: RequestContext, user_key: str) -> None:
        try:
            task = context.current_task
            if not task or not task.history:
                return
            for m in task.history:
                txt = get_message_text(m)
                if txt:
                    self.memory.ingest_text_for_pairs(user_key, txt)
        except Exception as e:
            logger.warning("pair history ingest failed: %s", e)

    async def _history_prefix(self, context: RequestContext) -> str:
        try:
            hist = []
            task = context.current_task
            if task and task.history:
                for m in task.history[-6:]:
                    role = m.role.value if hasattr(m.role, "value") else str(m.role)
                    text = get_message_text(m)
                    if text:
                        hist.append(f"{role}: {text}")
            joined = "\n".join(hist)[-2000:] if hist else ""
            return ("Prior conversation:\n" + joined) if joined else ""
        except Exception:
            return ""

    def _extract_text(self, resp: dict[str, Any]) -> str | None:
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
                ch = resp["choices"][0]
                return ch.get("message", {}).get("content")
        except Exception:
            pass
        return None
    
    def _maybe_answer_number_pair_query(self, context: RequestContext, user_key: str, user_text: str) -> str | None:
        if not user_text:
            return None
        m = self._pair_query_re.search(user_text)
        if not m:
            return None
        key = m.group(1)

        val = self.memory.find_pair(user_key, key)
        if val:
            return val

        val = self.memory.find_pair_any(key)
        if val:
            return val

        val = self._ad_hoc_find_in_history(context, key)
        if val:
            return val

        return None

    def _ad_hoc_find_in_history(self, context: RequestContext, key: str) -> str | None:
        """Parse visible history on the fly for pairs, without relying on persisted storage."""
        try:
            task = context.current_task
            if not task or not task.history:
                return None
            skey = str(key)
            local = {}
            for m in task.history:
                txt = get_message_text(m) or ""
                for rx in (self._pair_rx, self._pair_fallback_rx):
                    for mt in rx.finditer(txt):
                        a, b = mt.group(1), mt.group(2)
                        local[a] = b
                        local[b] = a
            return local.get(skey)
        except Exception:
            return None

    def _save_memory_note(self, user_key: str, text: str) -> None:
        m = re.search(r"(?is)\bmemory\s*note\s*:\s*(.+)$", text or "")
        if not m:
            return
        note = m.group(1).strip()[:2000]
        try:
            self.memory.append_note(user_key, note)
        except Exception:
            pass

    def _extract_plain_14_digits(self, text: str | None) -> str | None:
        """Accepts digit-ish sequences with spaces/hyphens, returns exactly 14 digits if present."""
        if not text:
            return None
        m = self._pure_14_re.search(text)
        if m:
            return m.group(1)
        for m in self._digitish_14_re.finditer(text):
            digits = re.sub(r"\D", "", m.group(0))
            if len(digits) == 14:
                return digits
        return None
    
    @staticmethod
    def _iter_image_parts(context) -> list[dict]:
        images = []
        try:
            if context.message and context.message.parts:
                for p in context.message.parts:
                    part = getattr(p, "root", p)
                    kind = getattr(part, "kind", None) or getattr(part, "type", None)
                    if kind not in ("file", "image"):
                        continue
                    f = getattr(part, "file", None)
                    if not f:
                        continue
                    mime = getattr(f, "mime_type", None) or getattr(f, "mimeType", None)
                    uri  = getattr(f, "uri", None) or getattr(f, "url", None)
                    name = getattr(f, "name", None) or getattr(f, "filename", None)
                    data = getattr(f, "bytes", None)

                    if not mime and name:
                        mime = mimetypes.guess_type(name)[0]
                    if not mime and data is not None:
                        mime = "image/png"

                    if uri:
                        images.append({"type": "input_image", "image_url": uri})
                        continue

                    if data is not None:
                        if isinstance(data, (bytes, bytearray)):
                            b64 = base64.b64encode(data).decode("ascii")
                        else:
                            b64 = str(data)
                        mime = mime or "image/png"
                        images.append({"type": "input_image", "image_url": f"data:{mime};base64,{b64}"})
        except Exception:
            pass
        return images
