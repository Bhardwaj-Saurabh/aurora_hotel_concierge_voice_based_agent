"""Tool schemas, deterministic routing, and the tool dispatcher (ADR-020 split).

Everything the brain exposes to the model lives here: the OpenAI-style TOOLS
schemas, the hybrid-routing phrase lists + required_tool_for (ADR-003), the
explicit-language-request gate (ADR-005), the guaranteed fallback/filler
strings (goal.md 2.2/2.5), and run_tool — the dispatcher mapping tool calls
onto bookings/knowledge/control actions.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from aurora.core.knowledge import search_hotel_knowledge
from aurora.storage.bookings import (
    ROOMS as _ROOMS,
    BookingValidationError,
    get_booking_backend,
    normalize_room_type as _normalize_room_type,
)

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
# hears dead air. One short, natural, spoken-friendly sentence — not a bare
# "one moment" filler word; it must not delay the real reply.
FILLER_MESSAGES = {
    "en": "Thanks for waiting, I'm working on that for you.",
    "es": "Gracias por esperar, estoy trabajando en ello.",
    "fr": "Merci de patienter, je m'en occupe.",
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

