"""
agent.py  -  the "brain" (Layer B). LLM + tool loop over a Provider.

Tools mirror a hotel reservations desk:
    check_availability -> find matching rooms
    create_booking     -> reserve a room
    transfer_to_human  -> front desk / human queue
    end_call           -> caller done (real system: SIP BYE)

Uses OpenAI-style function calling, which both Groq and OpenAI support, so this
file is provider-agnostic  -  it only talks to Provider.chat().
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from difflib import SequenceMatcher
from types import SimpleNamespace as NS

from bookings import (
    ROOMS as _ROOMS,
    BookingValidationError,
    get_booking_backend,
    normalize_room_type as _normalize_room_type,
)
from knowledge import search_hotel_knowledge
from providers import Provider
from router import AgentRouter, LANGUAGES
from telemetry import TurnTrace

SYSTEM_PROMPT = """You are a friendly phone reservations agent for Aurora Hotel.
Your only job is hotel room booking support: new reservations, availability,
room options, rates returned by tools, changing/canceling reservations, and
transferring to the front desk. Hotel policies and amenities are in scope even
when the caller asks about them during an incomplete booking flow.

Guardrails:
- Do not answer questions outside hotel booking support, including weather,
  news, trivia, coding, medical, legal, finance, or general assistant tasks.
- For off-topic requests, politely say you can only help with hotel reservations
  and ask whether they want to book, change, or cancel a stay.
- Never invent availability, rates, confirmation numbers, policies, or guest
  details. Use tools for availability and booking. Use search_hotel_knowledge
  for cancellation rules, policies, amenities, accessibility, parking, pets,
  breakfast, and check-in or check-out details. Use get_room_service_hours for
  room service or in-room dining hours. Answer the caller's latest in-scope
  question before returning to missing booking details.
- Keep replies short and spoken-friendly: one or two sentences, no bullet lists,
  no markdown, no emoji.
- When the caller asks to speak, continue, switch, or switch back in a supported
  language, call set_language immediately. Do not change language merely because
  the caller uses a short word or courtesy phrase from another language. After
  the tool result, answer in the selected language.

Booking flow:
1. First collect only check-in date, check-out date, guest count, and optional
   room type preference.
2. Once dates and guests are known, call check_availability immediately, even
   if no room type preference was given.
3. Offer the available room options and ask which one they want.
4. Only after the caller chooses or confirms a room, collect guest name and
   phone or email.
5. Before booking, summarize the selected room and ask for confirmation.
6. After the caller confirms and required details are present, call create_booking.
7. If the caller asks for a person or the request is outside what you can do,
   call transfer_to_human. When the conversation is clearly over, call end_call."""

# OpenAI-style tool schema (works on Groq too).
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "set_language",
            "description": "Set the response language for this call when the caller asks to speak, "
                           "continue, switch, or switch back in English, Spanish, or French. Only call "
                           "for an explicit language-change request, not an isolated foreign word or "
                           "courtesy.",
            "parameters": {
                "type": "object",
                "properties": {
                    "language": {
                        "type": "string",
                        "enum": ["en", "es", "fr"],
                        "description": "Requested response language: en for English, es for Spanish, "
                                       "or fr for French.",
                    },
                },
                "required": ["language"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_availability",
            "description": "Check hotel room availability for dates, guests, and optional room type.",
            "parameters": {
                "type": "object",
                "properties": {
                    "check_in": {
                        "type": "string",
                        "description": "Check-in date as stated by the caller.",
                    },
                    "check_out": {
                        "type": "string",
                        "description": "Check-out date as stated by the caller.",
                    },
                    "guests": {
                        "type": "integer",
                        "description": "Number of guests.",
                    },
                    "room_type": {
                        "type": "string",
                        "description": "Optional preference: standard, king, suite, family, or accessible.",
                    },
                },
                "required": ["check_in", "check_out", "guests"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_booking",
            "description": "Create a hotel booking after the caller confirms the room option.",
            "parameters": {
                "type": "object",
                "properties": {
                    "check_in": {"type": "string"},
                    "check_out": {"type": "string"},
                    "guests": {"type": "integer"},
                    "room_type": {"type": "string"},
                    "guest_name": {"type": "string"},
                    "contact": {
                        "type": "string",
                        "description": "Phone number or email for the booking.",
                    },
                },
                "required": [
                    "check_in",
                    "check_out",
                    "guests",
                    "room_type",
                    "guest_name",
                    "contact",
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_hotel_knowledge",
            "description": "Retrieve grounded Aurora Hotel policies, amenities, and operating details. "
                           "Always use for cancellation rules, check-in or check-out times, parking, "
                           "pets, breakfast, accessibility, and other hotel-information questions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The caller's policy or hotel-information question.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_room_service_hours",
            "description": "Get the hotel's room service operating hours for breakfast, lunch, "
                           "and dinner. Use for any question about room service or in-room "
                           "dining hours.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "transfer_to_human",
            "description": "Hand the call to a human agent queue. Use when the caller "
                           "asks for a person or the request is out of scope.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "end_call",
            "description": "End the call politely when the conversation is finished.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

_KNOWLEDGE_INTENT_PHRASES = (
    "cancellation policy", "cancelation policy", "cancellation fee", "cancel fee",
    "cancellation charge", "when can i cancel", "refundable", "non-refundable",
    "pet policy", "pets allowed", "dogs allowed", "bring my dog", "bring a pet",
    "parking", "valet", "breakfast", "check-in", "check in", "check-out",
    "check out", "accessibility", "accessible room", "wi-fi", "wifi", "amenities",
    "política de cancelación", "politica de cancelacion", "mascotas",
    "estacionamiento", "desayuno", "accesibilidad",
    "politique d'annulation", "animaux", "stationnement",
    "petit déjeuner", "petit dejeuner", "accessibilité", "accessibilite",
)

_FUZZY_AMENITY_TERMS = (
    "mascota", "mascotas", "pet", "pets", "parking", "estacionamiento",
    "breakfast", "desayuno", "accessibility", "accesibilidad", "wifi",
    "animaux", "stationnement", "dejeuner", "accessibilite",
)

# Guaranteed fallback strings (goal.md 2.2): spoken verbatim on provider failure,
# never generated by the model that just failed. Keyed by session language.
FALLBACK_RETRY_MESSAGES = {
    "en": "I'm sorry, I'm having trouble on my end. Could you say that again?",
    "es": "Lo siento, tengo un problema técnico. ¿Puede repetirlo, por favor?",
    "fr": "Désolé, je rencontre un problème technique. Pouvez-vous répéter, s'il vous plaît ?",
}
FALLBACK_TRANSFER_MESSAGES = {
    "en": "I'm still having trouble on my end. Let me connect you to the front desk.",
    "es": "Sigo teniendo problemas técnicos. Le comunico con la recepción.",
    "fr": "Je rencontre toujours un problème technique. Je vous transfère à la réception.",
}
# Spoken while a slow turn is still thinking (goal.md 2.5) so the caller never
# hears dead air. Short by design: it must not delay the real reply.
FILLER_MESSAGES = {
    "en": "One moment.",
    "es": "Un momento.",
    "fr": "Un instant.",
}

# Normalized (accent-stripped) tokens; each set names that language in EN/ES/FR.
_LANGUAGE_NAMES = {
    "en": {"english", "ingles", "anglais"},
    "es": {"spanish", "espanol", "espagnol"},
    "fr": {"french", "francais", "frances"},
}


def _normalized_tokens(text: str) -> list[str]:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    normalized = "".join(
        character for character in decomposed
        if not unicodedata.combining(character)
    )
    return re.findall(r"[a-z0-9]+", normalized)


def _has_fuzzy_term(tokens: list[str], terms: tuple[str, ...], cutoff: float = 0.82) -> bool:
    return any(
        SequenceMatcher(None, token, term).ratio() >= cutoff
        for token in tokens
        for term in terms
    )


def explicit_language_request(text: str, language: str) -> bool:
    """Require the target language name before allowing a session-state change."""
    return bool(set(_normalized_tokens(text)) & _LANGUAGE_NAMES.get(language, set()))


def required_tool_for(text: str) -> str | None:
    """Route high-confidence knowledge intents before probabilistic LLM selection."""
    normalized = " ".join(text.lower().split())
    if any(phrase in normalized for phrase in _KNOWLEDGE_INTENT_PHRASES):
        return "search_hotel_knowledge"
    tokens = _normalized_tokens(text)
    if _has_fuzzy_term(tokens, _FUZZY_AMENITY_TERMS):
        return "search_hotel_knowledge"
    has_policy = _has_fuzzy_term(tokens, ("policy", "politica", "politique"))
    has_cancellation = _has_fuzzy_term(tokens, ("cancellation", "cancelacion", "annulation"))
    if has_policy and has_cancellation:
        return "search_hotel_knowledge"
    return None


def _named_tool_choice(name: str) -> dict:
    return {"type": "function", "function": {"name": name}}


def _cancel_requested(cancel) -> bool:
    """Cooperative barge-in signal (goal.md 3.3): a threading.Event or None."""
    return cancel is not None and cancel.is_set()


# --- Tool implementations (availability is mock; bookings persist via bookings.py) ---


def run_tool(name: str, args: dict, session_id: str = "") -> dict:
    """Execute a tool call. The optional 'action' key is a control signal for
    the voice loop ('transfer' -> SIP REFER, 'hangup' -> SIP BYE)."""
    if name == "check_availability":
        guests = int(args.get("guests") or 1)
        preferred = _normalize_room_type(args.get("room_type"))
        rooms = []
        for key, room in _ROOMS.items():
            if preferred and key != preferred:
                continue
            if guests <= room["capacity"]:
                rooms.append(f"{room['name']} at {room['rate']}")
        if not rooms:
            return {
                "result": "No matching rooms are available for that guest count. "
                          "Offer to transfer to the front desk.",
            }
        return {
            "result": "Available rooms for "
                      f"{args.get('check_in')} to {args.get('check_out')}: "
                      f"{'; '.join(rooms)}.",
        }
    if name == "create_booking":
        try:
            record = get_booking_backend().create_booking(
                session_id=session_id,
                check_in=str(args.get("check_in", "")),
                check_out=str(args.get("check_out", "")),
                guests=args.get("guests", 0),
                room_type=args.get("room_type"),
                guest_name=str(args.get("guest_name", "")),
                contact=str(args.get("contact", "")),
            )
        except BookingValidationError as exc:
            return {
                "result": f"The booking could not be created: {exc} "
                          "Correct the details with the caller, or offer the front desk.",
            }
        room = _ROOMS[record.room_type]
        if record.created:
            return {
                "result": f"Booking confirmed. Confirmation {record.confirmation_id} for "
                          f"{record.guest_name} in a {room['name']} from "
                          f"{record.check_in} to {record.check_out} for "
                          f"{record.guests} guest(s). Confirmation sent to "
                          f"{record.contact}.",
            }
        return {
            "result": f"This booking is already confirmed. Confirmation "
                      f"{record.confirmation_id} for {record.guest_name} in a "
                      f"{room['name']} from {record.check_in} to {record.check_out}. "
                      "No duplicate booking was created.",
        }
    if name == "get_room_service_hours":
        # Breakfast window matches hotel_policies.md#Breakfast (one truth, two doors).
        return {
            "result": "Room service hours: breakfast from 6:30 AM to 10:30 AM, lunch from "
                      "11:30 AM to 2:30 PM, and dinner from 5:00 PM to 10:00 PM.",
        }
    if name == "search_hotel_knowledge":
        return search_hotel_knowledge(str(args.get("query", "")))
    if name == "transfer_to_human":
        return {"result": "Transferring you to the front desk.", "action": "transfer"}
    if name == "end_call":
        return {"result": "Ending the call.", "action": "hangup"}
    return {"result": f"Unknown tool: {name}"}


class Agent:
    """LLM + tool loop for one call. Holds conversation history."""

    def __init__(self, provider: Provider):
        self.provider = provider
        self.messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
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
            self.messages[0]["content"] = f"{SYSTEM_PROMPT}\n\n{self.router.instruction()}"
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
                                f"{SYSTEM_PROMPT}\n\n{self.router.instruction()}"
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
