"""
agent.py  -  the "brain" (Layer B). LLM + tool loop over a Provider.

Uses OpenAI-style function calling, which both Groq and OpenAI support, so this
file is provider-agnostic  -  it only talks to Provider.chat(). Tool schemas,
routing, and the dispatcher live in aurora.core.tools; the system prompt in
aurora.core.prompts (ADR-020).
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace as NS

from aurora.core.prompts import SYSTEM_PROMPT
from aurora.core.providers import Provider
from aurora.core.router import AgentRouter, LANGUAGES
from aurora.core.tools import (
    FALLBACK_RETRY_MESSAGES,
    FALLBACK_TRANSFER_MESSAGES,
    FILLER_MESSAGES,
    TOOLS,
    explicit_language_request,
    required_tool_for,
    run_tool,
)
from aurora.telemetry.traces import TurnTrace

__all__ = [
    "Agent",
    "SYSTEM_PROMPT",
    "TOOLS",
    "FALLBACK_RETRY_MESSAGES",
    "FALLBACK_TRANSFER_MESSAGES",
    "FILLER_MESSAGES",
    "explicit_language_request",
    "required_tool_for",
    "run_tool",
]

def _named_tool_choice(name: str) -> dict:
    return {"type": "function", "function": {"name": name}}


def _cancel_requested(cancel) -> bool:
    """Cooperative barge-in signal (goal.md 3.3): a threading.Event or None."""
    return cancel is not None and cancel.is_set()


class Agent:
    """LLM + tool loop for one call. Holds conversation history."""

    def __init__(self, provider: Provider):
        from aurora.prompt_registry import get_system_prompt

        self.provider = provider
        self.system_prompt, self.prompt_version = get_system_prompt(SYSTEM_PROMPT)
        self.messages: list[dict] = [{"role": "system", "content": self.system_prompt}]
        self.router = AgentRouter()
        self.current_language = "en"
        self.current_locale = LANGUAGES["en"]["locale"]
        self.last_trace: TurnTrace | None = None
        self.last_sources: list[str] = []
        self.last_action: str | None = None
        self._consecutive_llm_failures = 0

    def respond(self, user_text: str, trace: TurnTrace | None = None) -> tuple[str, str | None]:
        """Take the caller's transcript, return (spoken_reply, action|None).

        Joins the streaming path (goal.md 3.2), so text mode, the HTTP bridge,
        the smoke test, and every eval gate the streaming refactor by
        construction.
        """
        reply = "".join(self.respond_stream(user_text, trace=trace))
        return reply, self.last_action

    def respond_stream(self, user_text: str, trace: TurnTrace | None = None,
                       cancel=None):
        """Yield the spoken reply incrementally; the final control action lands
        in `self.last_action`.

        Loops until the model produces a plain text reply, executing any tool
        calls in between. With a streaming provider (`stream_chat`), content
        deltas are yielded as they arrive so TTS can start before the full
        reply exists; the `llm` timing then records time-to-first-token — the
        latency that matters for voice.

        `cancel` (a threading.Event) is the barge-in signal (goal.md 3.3):
        once set, the provider stream is closed (stop paying for tokens),
        pending tool calls get synthetic responses (history stays OpenAI-valid),
        and history keeps only what the caller actually heard.
        """
        trace = trace or TurnTrace()
        self.last_trace = trace
        self.last_sources = []
        self.last_action = None

        with trace.span("routing"):
            route = self.router.route()
            self.current_language = route.language
            self.current_locale = route.locale
            self.messages[0]["content"] = f"{self.system_prompt}\n\n{self.router.instruction()}"
        trace.event(
            "router.selected",
            language=route.language,
            locale=route.locale,
            changed=route.changed,
            reason=route.reason,
        )
        trace.attributes.update({
            "language": route.language,
            "locale": route.locale,
            "provider": getattr(self.provider, "name", "unknown"),
            "model": getattr(self.provider, "llm_model", "unknown"),
            "promptVersion": self.prompt_version,
        })
        trace.event("caller.transcript", text=user_text)
        self.messages.append({"role": "user", "content": user_text})
        action: str | None = None
        required_tool = required_tool_for(user_text)
        if required_tool:
            trace.event(
                "tool.route_selected",
                tool=required_tool,
                reason="hotel_knowledge_intent",
            )
        first_model_call = True

        while True:
            if _cancel_requested(cancel):
                self._close_cancelled(trace, "")
                return
            tool_choice = (
                _named_tool_choice(required_tool)
                if first_model_call and required_tool
                else None
            )
            stream_fn = getattr(self.provider, "stream_chat", None)
            content_parts: list[str] = []
            try:
                if stream_fn is None:
                    # Batch path (MockProvider and any non-streaming backend).
                    with trace.span("llm", model=getattr(self.provider, "llm_model", "unknown")):
                        resp = self.provider.chat(
                            self.messages,
                            tools=TOOLS,
                            tool_choice=tool_choice,
                        )
                    msg = resp.choices[0].message
                    content = msg.content or ""
                    tool_calls = list(msg.tool_calls or [])
                    if content and not tool_calls:
                        content_parts.append(content)
                        yield content
                else:
                    # Streaming path (goal.md 3.2): yield content deltas as they
                    # arrive; assemble tool-call fragments by index.
                    llm_started = time.perf_counter()
                    assembled: dict[int, dict] = {}
                    first_token_at = None
                    cancelled_mid_stream = False
                    stream = stream_fn(self.messages, tools=TOOLS, tool_choice=tool_choice)
                    for chunk in stream:
                        if _cancel_requested(cancel):
                            cancelled_mid_stream = True
                            break
                        delta = chunk.choices[0].delta
                        for tc_delta in (getattr(delta, "tool_calls", None) or []):
                            entry = assembled.setdefault(
                                tc_delta.index, {"id": None, "name": "", "arguments": ""}
                            )
                            if getattr(tc_delta, "id", None):
                                entry["id"] = tc_delta.id
                            function = getattr(tc_delta, "function", None)
                            if function is not None:
                                if getattr(function, "name", None):
                                    entry["name"] = function.name
                                if getattr(function, "arguments", None):
                                    entry["arguments"] += function.arguments
                        piece = getattr(delta, "content", None)
                        if piece:
                            if first_token_at is None:
                                first_token_at = time.perf_counter()
                                trace.event(
                                    "llm.first_token",
                                    ttftMs=round((first_token_at - llm_started) * 1000, 1),
                                )
                            content_parts.append(piece)
                            yield piece
                    if cancelled_mid_stream:
                        # Close the provider stream: no more tokens billed for
                        # a reply nobody is listening to.
                        getattr(stream, "close", lambda: None)()
                        self._close_cancelled(trace, "".join(content_parts))
                        return
                    # For voice, time-to-first-token IS the llm latency; a pure
                    # tool-call turn (no content) records its full duration.
                    llm_ended = first_token_at or time.perf_counter()
                    trace.set_timing(
                        "llm",
                        trace.timings.get("llm", 0.0) + (llm_ended - llm_started) * 1000,
                    )
                    content = "".join(content_parts)
                    tool_calls = [
                        NS(
                            id=entry["id"] or f"call_{index}",
                            type="function",
                            function=NS(name=entry["name"], arguments=entry["arguments"]),
                        )
                        for index, entry in sorted(assembled.items())
                    ]
                first_model_call = False
            except Exception as exc:
                # Guaranteed fallback (goal.md 2.2): never crash the call, never
                # dead-end the caller. One bad turn re-prompts; two transfer.
                self._consecutive_llm_failures += 1
                trace.event(
                    "llm.fallback",
                    errorType=type(exc).__name__,
                    consecutiveFailures=self._consecutive_llm_failures,
                )
                if self._consecutive_llm_failures >= 2:
                    reply = FALLBACK_TRANSFER_MESSAGES.get(
                        self.current_language, FALLBACK_TRANSFER_MESSAGES["en"]
                    )
                    trace.event("failure.transfer", reason="repeated_llm_failures")
                    action = "transfer"
                else:
                    reply = FALLBACK_RETRY_MESSAGES.get(
                        self.current_language, FALLBACK_RETRY_MESSAGES["en"]
                    )
                # History keeps whatever was already spoken plus the fallback.
                spoken = " ".join(
                    part for part in ("".join(content_parts), reply) if part
                ).strip()
                self.messages.append({"role": "assistant", "content": spoken})
                trace.event("assistant.response", text=spoken, action=action)
                self.last_action = action
                yield reply
                return
            self._consecutive_llm_failures = 0

            if not tool_calls:
                self.messages.append({"role": "assistant", "content": content})
                trace.event("assistant.response", text=content, action=action)
                self.last_action = action
                return

            if _cancel_requested(cancel):
                # Interrupted before the tool turn was committed: skip it whole.
                self._close_cancelled(trace, content)
                return

            # Record the assistant's tool-call turn, then answer each call.
            self.messages.append({
                "role": "assistant",
                "content": content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name,
                                     "arguments": tc.function.arguments},
                    }
                    for tc in tool_calls
                ],
            })
            batch_action = self._run_tool_calls(tool_calls, trace, user_text, cancel)
            if batch_action:
                action = batch_action
            # loop again so the model can speak given the tool results

    def _close_cancelled(self, trace: TurnTrace, spoken: str) -> None:
        """Barge-in bookkeeping: history keeps only what the caller heard."""
        spoken = spoken.strip()
        if spoken:
            self.messages.append({"role": "assistant", "content": spoken})
        trace.event("turn.cancelled", reason="barge_in", spokenChars=len(spoken))
        self.last_action = None

    def _run_tool_calls(self, tool_calls, trace: TurnTrace, user_text: str,
                        cancel=None) -> str | None:
        """Execute one batch of tool calls; return the batch's control action.

        Once the assistant tool-call turn is committed, every call id MUST get
        a tool response (OpenAI history invariant). A barge-in mid-batch never
        interrupts a running tool; the not-yet-started ones get synthetic
        cancelled responses instead.
        """
        action: str | None = None
        for tc in tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                if _cancel_requested(cancel):
                    trace.event("tool.cancelled", tool=tc.function.name)
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": "Cancelled: the caller interrupted before this tool ran.",
                    })
                    continue
                trace.event("tool.requested", tool=tc.function.name, arguments=args)
                with trace.span("tools", tool=tc.function.name):
                    if tc.function.name == "set_language":
                        language = str(args.get("language", "")).lower()
                        try:
                            if not explicit_language_request(user_text, language):
                                trace.event(
                                    "router.language_change_rejected",
                                    requestedLanguage=language,
                                    reason="no_explicit_language_name",
                                )
                                raise PermissionError
                            language_route = self.router.set_language(language)
                            self.current_language = language_route.language
                            self.current_locale = language_route.locale
                            self.messages[0]["content"] = (
                                f"{self.system_prompt}\n\n{self.router.instruction()}"
                            )
                            trace.attributes.update({
                                "language": language_route.language,
                                "locale": language_route.locale,
                            })
                            trace.event(
                                "router.language_changed",
                                language=language_route.language,
                                locale=language_route.locale,
                                changed=language_route.changed,
                                reason=language_route.reason,
                            )
                            result = {
                                "result": (
                                    "Response language set to "
                                    f"{LANGUAGES[language_route.language]['name']}."
                                ),
                            }
                        except PermissionError:
                            result = {
                                "result": (
                                    "Language unchanged because the caller did not explicitly "
                                    "request the target language. Continue in the current language."
                                ),
                            }
                        except ValueError:
                            result = {
                                "result": "Unsupported language. Continue in the current language.",
                            }
                    elif tc.function.name == "search_hotel_knowledge":
                        with trace.span("retrieval", query=args.get("query", "")):
                            result = run_tool(tc.function.name, args, session_id=trace.session_id)
                    else:
                        result = run_tool(tc.function.name, args, session_id=trace.session_id)
                trace.event(
                    "tool.result",
                    tool=tc.function.name,
                    result=result.get("result", ""),
                    sources=result.get("sources", []),
                    action=result.get("action"),
                )
                self.last_sources.extend(result.get("sources", []))
                if result.get("action"):
                    action = result["action"]
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result["result"],
                })
        return action
