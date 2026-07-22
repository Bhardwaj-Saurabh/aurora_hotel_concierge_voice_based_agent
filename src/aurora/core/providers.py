"""
providers.py  -  one adaptor, two backends: Groq and OpenAI.

Groq speaks the OpenAI API dialect, so a single code path covers both  -  only
base_url, api_key, and model names differ. Switch with PROVIDER=groq|openai in
.env; move to your OpenAI key later by flipping that one value.

Exposes three stages the voice loop needs:
    chat(messages, tools)        -> LLM turn (OpenAI-style tool calling)
    transcribe(pcm_int16, rate)  -> STT (Whisper)
    synthesize(text)             -> TTS; returns WAV bytes, or None if it
                                    already played via the system voice command
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import wave
from types import SimpleNamespace as NS

# Sensible defaults per backend. Any of these can be overridden in .env.
PRESETS = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "api_key_env": "GROQ_API_KEY",
        # 70b = reliable tool-calling; swap to llama-3.1-8b-instant for lower latency.
        "llm_model": "llama-3.3-70b-versatile",
        "stt_model": "whisper-large-v3-turbo",
        "tts_model": "canopylabs/orpheus-v1-english",
        "tts_voice": "troy",
        # Orpheus's speech endpoint has unconfirmed support for a numeric speed
        # control; leave it at the API default until that's verified live.
        "tts_speed": "1.0",
    },
    "openai": {
        "base_url": None,                # SDK default endpoint
        "api_key_env": "OPENAI_API_KEY",
        "llm_model": "gpt-4o-mini",
        "stt_model": "whisper-1",
        "tts_model": "tts-1",
        "tts_voice": "alloy",
        # ~10% brisker than the API default (1.0), per a live debugging pass
        # (2026-07-22): the default pace read as sluggish over the phone.
        "tts_speed": "1.1",
    },
}

DEFAULT_STT_PROMPT = (
    "Aurora Hotel reservations conversation in English, Spanish, or French. "
    "Hotel vocabulary: reservation, booking, check-in, check-out, cancellation policy, "
    "pet policy, parking, breakfast, accessibility, habitación, reserva, política de "
    "cancelación, mascotas, estacionamiento, desayuno, accesibilidad, chambre, "
    "réservation, politique d'annulation, animaux, stationnement, petit déjeuner, "
    "accessibilité. Callers may ask to switch to English, Spanish, or French, or say "
    "español or français. Callers often spell out an email address aloud, for example "
    "john dot smith at gmail dot com, or jane underscore doe at yahoo dot com."
)


def _env_or_default(key: str, default: str) -> str:
    """Return a non-empty environment override or the provider preset.

    A copied .env template can leave a comment after an empty assignment.
    Some dotenv versions preserve that comment as the value, which would send
    an invalid model ID to the provider.
    """
    value = os.getenv(key, "").strip()
    if not value or value.startswith("#"):
        return default
    return value


class Provider:
    """Configured client for one backend. Read from .env on construction."""

    def __init__(self, name: str | None = None):
        name = (name or os.getenv("PROVIDER", "groq")).lower()
        if name not in PRESETS:
            raise ValueError(f"Unknown PROVIDER {name!r}; use one of {list(PRESETS)}")
        self.name = name
        p = PRESETS[name]

        api_key = os.getenv(p["api_key_env"])
        if not api_key:
            raise RuntimeError(f"Set {p['api_key_env']} in your .env (PROVIDER={name})")
        from openai import OpenAI  # lazy: the mock path needs no SDK installed
        # Bounded waits + one transport retry (goal.md 2.2); the agent's guaranteed
        # fallback strings handle whatever still fails after this.
        self.client = OpenAI(
            api_key=api_key,
            base_url=p["base_url"],
            timeout=float(_env_or_default("PROVIDER_TIMEOUT_S", "30")),
            max_retries=int(_env_or_default("PROVIDER_MAX_RETRIES", "1")),
        )

        # Per-stage overrides fall back to the preset.
        self.llm_model = _env_or_default("LLM_MODEL", p["llm_model"])
        # Lowered from 0.3 (live debugging pass, 2026-07-22): measured 3/8
        # off-topic-guardrail misfires at 0.3 on an indirect, non-English
        # booking phrase ("a place to stay" rather than "a room"); 0/8 at
        # 0.15 across the same batch. Tool-call classification benefits from
        # determinism more than reply variety does.
        self.llm_temperature = float(_env_or_default("LLM_TEMPERATURE", "0.15"))
        self.stt_model = _env_or_default("STT_MODEL", p["stt_model"])
        self.stt_prompt = _env_or_default("STT_PROMPT", DEFAULT_STT_PROMPT)
        self.tts_model = _env_or_default("TTS_MODEL", p["tts_model"])
        self.tts_voice = _env_or_default("TTS_VOICE", p["tts_voice"])
        self.tts_speed = float(_env_or_default("TTS_SPEED", p["tts_speed"]))
        self.tts_instructions = os.getenv("TTS_INSTRUCTIONS")
        # "provider" = cloud TTS; "system" = local system voice command.
        self.tts_backend = os.getenv("TTS_BACKEND", "provider").lower()

    # --- LLM ---
    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice=None,
    ):
        """One chat-completion call. Returns the raw SDK response."""
        return self.client.chat.completions.create(
            model=self.llm_model,
            messages=messages,
            tools=tools or None,
            tool_choice=(tool_choice or "auto") if tools else None,
            temperature=self.llm_temperature,
        )

    def stream_chat(self, messages: list[dict], tools: list[dict] | None = None,
                    tool_choice=None):
        """One chat turn as a stream of raw SDK chunks (goal.md 3.2).

        The agent yields content deltas to TTS as they arrive; MockProvider has
        no stream_chat, so the offline path uses chat() unchanged.
        """
        return self.client.chat.completions.create(
            model=self.llm_model,
            messages=messages,
            tools=tools or None,
            tool_choice=(tool_choice or "auto") if tools else None,
            temperature=self.llm_temperature,
            stream=True,
        )

    # --- STT ---
    def transcribe(self, pcm_int16: bytes, sample_rate: int = 16000) -> str:
        """Transcribe raw 16-bit mono PCM via Whisper."""
        wav = _pcm_to_wav(pcm_int16, sample_rate)
        wav.name = "turn.wav"  # SDK infers format from the filename
        transcription_args = {
            "model": self.stt_model,
            "file": wav,
            "response_format": "text",
        }
        if self.stt_prompt:
            transcription_args["prompt"] = self.stt_prompt
        resp = self.client.audio.transcriptions.create(
            **transcription_args,
        )
        return (resp if isinstance(resp, str) else resp.text).strip()

    # --- TTS ---
    def synthesize(self, text: str) -> bytes | None:
        """Return WAV bytes for `text`, or None if played directly by the OS."""
        if self.tts_backend == "system":
            subprocess.run([os.getenv("SYSTEM_TTS_CMD", "say"), text], check=False)
            return None
        speech_args = {
            "model": self.tts_model,
            "voice": self.tts_voice,
            "input": text,
            "response_format": "wav",
            "speed": self.tts_speed,
        }
        if self.tts_instructions:
            speech_args["instructions"] = self.tts_instructions
        resp = self.client.audio.speech.create(
            **speech_args,
        )
        return resp.content


# --- audio helpers ---

def _pcm_to_wav(pcm_int16: bytes, sample_rate: int) -> io.BytesIO:
    """Wrap raw 16-bit mono PCM samples into an in-memory WAV file."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)  # 16-bit
        w.setframerate(sample_rate)
        w.writeframes(pcm_int16)
    buf.seek(0)
    return buf


# --- Mock backend: full offline end-to-end, no network / key / SDK ---

class MockProvider:
    """Drop-in stand-in for Provider. Rule-based LLM, scripted STT, no-op TTS.

    Same interface (chat / transcribe / synthesize) so the voice loop and
    agent.py can't tell the difference. Use for rehearsals, CI, and testing the
    loop without touching Groq/OpenAI. Enable with PROVIDER=mock.
    """

    name = "mock"

    def __init__(self):
        self.llm_model = "mock-llm"
        self.stt_model = "mock-stt"
        self.tts_model = "mock-tts"
        self.tts_voice = "mock"
        self.tts_backend = os.getenv("TTS_BACKEND", "print").lower()
        # Scripted transcripts for mic mode (there's no offline STT); cycles.
        self._stt_script = [
            "I need a room from August 12 to August 14 for two guests.",
            "Book it for Priya Shah, priya@example.com.",
            "Can I speak to a person?",
            "Goodbye",
        ]
        self._stt_i = 0

    def chat(self, messages: list[dict], tools=None, tool_choice=None):
        """Rule-based reply mimicking OpenAI-style tool calling."""
        last = messages[-1]
        system_content = messages[0].get("content", "")
        if "Current response language: Spanish" in system_content:
            language = "es"
        elif "Current response language: French" in system_content:
            language = "fr"
        else:
            language = "en"
        spanish = language == "es"
        french = language == "fr"
        forced_tool = _tool_choice_name(tool_choice)
        if forced_tool == "search_hotel_knowledge" and last.get("role") == "user":
            return _mk_tool(forced_tool, {"query": last.get("content") or ""})
        # After a tool ran, speak a reply built from its result.
        if last.get("role") == "tool":
            result = last["content"]
            if result.lower().startswith("response language set to spanish"):
                original = _last_user_text(messages).lower()
                if _mock_knowledge_request(original):
                    return _mk_tool("search_hotel_knowledge", {"query": original})
                if _mock_off_topic(original):
                    return _mk_text("Solo puedo ayudar con reservas de hotel. ¿Quiere reservar, cambiar o cancelar una estancia?")
                return _mk_text("Claro. Puedo ayudarle con una reserva en Aurora Hotel.")
            if result.lower().startswith("response language set to english"):
                original = _last_user_text(messages).lower()
                if _mock_knowledge_request(original):
                    return _mk_tool("search_hotel_knowledge", {"query": original})
                if _mock_off_topic(original):
                    return _mk_text("I can only help with hotel reservations. Are you looking to book, change, or cancel a stay?")
                return _mk_text("Of course. I can continue in English with your Aurora Hotel reservation.")
            if result.lower().startswith("response language set to french"):
                original = _last_user_text(messages).lower()
                if _mock_knowledge_request(original):
                    return _mk_tool("search_hotel_knowledge", {"query": original})
                if _mock_off_topic(original):
                    return _mk_text("Je peux uniquement vous aider avec les réservations d'hôtel. Souhaitez-vous réserver, modifier ou annuler un séjour ?")
                return _mk_text("Bien sûr. Je peux vous aider avec votre réservation à l'hôtel Aurora.")
            if result.lower().startswith("available rooms"):
                if spanish:
                    return _mk_text(f"{result} ¿Quiere que reserve una de estas habitaciones?")
                if french:
                    return _mk_text(f"{result} Souhaitez-vous que je réserve l'une de ces chambres ?")
                return _mk_text(f"{result} Would you like me to book one of these?")
            if result.lower().startswith("booking confirmed"):
                confirmation = re.search(r"AH-\d+", result)
                if spanish:
                    code = confirmation.group(0) if confirmation else "confirmada"
                    return _mk_text(f"La reserva está confirmada. Su número de confirmación es {code}.")
                if french:
                    code = confirmation.group(0) if confirmation else "confirmée"
                    return _mk_text(f"La réservation est confirmée. Votre numéro de confirmation est {code}.")
                return _mk_text(result)
            if result.lower().startswith("grounded hotel knowledge"):
                tool_args = _previous_tool_arguments(messages)
                return _mk_text(_grounded_policy_reply(result, language, tool_args.get("query", "")))
            if result.lower().startswith("transferring") and spanish:
                return _mk_text("Le transfiero a la recepción.")
            if result.lower().startswith("transferring") and french:
                return _mk_text("Je vous transfère à la réception.")
            if result.lower().startswith("ending") and spanish:
                return _mk_text("Gracias por llamar a Aurora Hotel. Adiós.")
            if result.lower().startswith("ending") and french:
                return _mk_text("Merci d'avoir appelé l'hôtel Aurora. Au revoir.")
            return _mk_text(result)  # transfer / hangup / not-found: speak as-is

        text = (last.get("content") or "").lower()
        tokens = set(re.findall(r"[\wáéíóúüñ]+", text, flags=re.UNICODE))
        if any(phrase in text for phrase in (
            "speak spanish", "switch to spanish", "spanish please", "habla español",
            "hable español", "en español",
        )):
            return _mk_tool("set_language", {"language": "es"})
        if any(phrase in text for phrase in (
            "speak english", "switch to english", "switch back to english",
            "back to english", "return to english", "english please", "english again",
            "habla inglés", "hable inglés", "en inglés", "habla ingles", "en anglais",
        )):
            return _mk_tool("set_language", {"language": "en"})
        if any(phrase in text for phrase in (
            "speak french", "switch to french", "french please", "in french",
            "parle français", "parlez français", "en français",
            "parle francais", "parlez francais", "en francais",
        )):
            return _mk_tool("set_language", {"language": "fr"})
        if "room service" in text or "in-room dining" in text:
            return _mk_tool("get_room_service_hours", {})
        if _mock_knowledge_request(text):
            return _mk_tool("search_hotel_knowledge", {"query": last.get("content") or ""})
        if any(w in text for w in ("bye", "goodbye", "that's all", "thats all",
                                   "nothing else", "no thanks", "hang up", "adiós", "adios",
                                   "au revoir")):
            return _mk_tool("end_call", {})
        if _mock_off_topic(text):
            if spanish:
                return _mk_text("Solo puedo ayudar con reservas de hotel. ¿Quiere reservar, cambiar o cancelar una estancia?")
            if french:
                return _mk_text("Je peux uniquement vous aider avec les réservations d'hôtel. Souhaitez-vous réserver, modifier ou annuler un séjour ?")
            return _mk_text("I can only help with hotel reservations. Are you looking to book, change, or cancel a stay?")
        if any(phrase in text for phrase in (
            "another reservation", "another guest", "other guest", "someone else's",
        )):
            if spanish:
                return _mk_text("No puedo revelar datos de otro huésped. Solo puedo ayudar con su propia reserva de hotel.")
            return _mk_text("I cannot disclose another guest's information. I can only help with your own hotel reservation.")
        if tokens & {"human", "person", "representative", "agent", "operator", "persona", "recepción"}:
            return _mk_tool("transfer_to_human", {})
        if any(w in text for w in ("change", "cancel", "modify", "front desk")):
            return _mk_tool("transfer_to_human", {})
        if any(w in text for w in ("book", "reserve", "yes", "confirm", "reservar", "confirmo")) and any(
            w in text for w in ("name", "email", "@", "phone", "priya", "shah", "nombre")
        ):
            return _mk_tool("create_booking", {
                "check_in": "August 12",
                "check_out": "August 14",
                "guests": 2,
                "room_type": "standard",
                "guest_name": "Priya Shah",
                "contact": "priya@example.com",
            })
        if any(w in text for w in (
            "room", "hotel", "stay", "book", "reservation", "guests", "guest",
            "habitación", "habitacion", "reserva", "personas", "huéspedes", "huespedes",
            "chambre", "personnes", "réservation",
        )):
            return _mk_tool("check_availability", {
                "check_in": "August 12",
                "check_out": "August 14",
                "guests": 2,
                "room_type": "standard",
            })
        if spanish:
            return _mk_text("Solo puedo ayudar con reservas de hotel. ¿Quiere reservar, cambiar o cancelar una estancia?")
        if french:
            return _mk_text("Je peux uniquement vous aider avec les réservations d'hôtel. Souhaitez-vous réserver, modifier ou annuler un séjour ?")
        return _mk_text("I can help with hotel reservations only. Would you like to book, change, or cancel a stay?")

    def transcribe(self, pcm_int16: bytes, sample_rate: int = 16000) -> str:
        """No offline STT  -  return the next scripted phrase (rehearsal mode)."""
        phrase = self._stt_script[self._stt_i % len(self._stt_script)]
        self._stt_i += 1
        return phrase

    def synthesize(self, text: str) -> bytes | None:
        """No cloud TTS. Optionally use a local voice command; else print-only."""
        if self.tts_backend == "system":
            subprocess.run([os.getenv("SYSTEM_TTS_CMD", "say"), text], check=False)
        return None  # voice_loop already prints the agent's text


def _mk_text(content: str):
    return NS(choices=[NS(message=NS(content=content, tool_calls=None))])


def _mk_tool(name: str, args: dict):
    tc = NS(id=f"call_{name}", type="function",
            function=NS(name=name, arguments=json.dumps(args)))
    return NS(choices=[NS(message=NS(content=None, tool_calls=[tc]))])


def _tool_choice_name(tool_choice) -> str | None:
    if not isinstance(tool_choice, dict):
        return None
    function = tool_choice.get("function") or {}
    return function.get("name")


def _last_user_text(messages: list[dict]) -> str:
    return next(
        (message.get("content") or "" for message in reversed(messages) if message.get("role") == "user"),
        "",
    )


def _mock_knowledge_request(text: str) -> bool:
    return any(word in text for word in (
        "cancellation policy", "cancel policy", "check-in", "check in", "check-out",
        "check out", "parking", "pets", "pet policy", "breakfast", "accessible",
        "accessibility", "policy", "estacionamiento", "mascotas", "desayuno",
        "annulation", "animaux", "stationnement", "petit déjeuner", "petit dejeuner",
        "accessibilité", "accessibilite", "politique",
    ))


def _mock_off_topic(text: str) -> bool:
    return any(word in text for word in (
        "weather", "news", "sports", "stock", "joke", "trivia", "clima", "noticias",
        # Medical / legal / financial advice stays out of scope (mirrors SYSTEM_PROMPT).
        # "taxes" not "tax" (substring would catch "taxi").
        "medical", "medication", "medicine", "doctor", "diagnosis", "symptom",
        "legal", "lawyer", "attorney", "sue the", "lawsuit", "loan", "mortgage",
        "invest", "crypto", "taxes", "médico", "medicamento", "abogado", "demanda",
        "préstamo", "prestamo", "invertir", "impuestos",
    ))


def _previous_tool_arguments(messages: list[dict]) -> dict:
    if len(messages) < 2:
        return {}
    calls = messages[-2].get("tool_calls") or []
    if not calls:
        return {}
    try:
        return json.loads(calls[0]["function"].get("arguments") or "{}")
    except (json.JSONDecodeError, KeyError, TypeError):
        return {}


def _grounded_policy_reply(result: str, language: str, query: str) -> str:
    topic = query.lower()
    spanish = language == "es"
    french = language == "fr"
    if "cancel" in topic or "annulation" in topic:
        if spanish:
            return "Puede cancelar sin cargo hasta las 6:00 PM, hora local del hotel, dos días antes de la llegada. Las tarifas promocionales prepagadas no son reembolsables."
        if french:
            return "Vous pouvez annuler sans frais jusqu'à 18h00, heure locale de l'hôtel, deux jours avant l'arrivée. Les tarifs promotionnels prépayés ne sont pas remboursables."
        return "You may cancel without charge until 6:00 PM local hotel time two days before arrival. Prepaid promotional rates are non-refundable."
    if "parking" in topic or "estacionamiento" in topic or "stationnement" in topic:
        if spanish:
            return "El estacionamiento cuesta $28 por noche y el servicio de valet cuesta $42 por noche."
        if french:
            return "Le stationnement coûte 28 $ par nuit et le service de voiturier coûte 42 $ par nuit."
        return "Self-parking is $28 per night, and valet parking is $42 per night."
    if "pet" in topic or "dog" in topic or "mascota" in topic or "animaux" in topic or "chien" in topic:
        if spanish:
            return "Se permiten hasta dos perros por habitación, con un límite de 50 libras por perro y una tarifa de limpieza de $75 por estancia."
        if french:
            return "Deux chiens maximum sont admis par chambre, avec une limite de 50 livres par chien et des frais de nettoyage de 75 $ par séjour."
        return "Up to two dogs are allowed per room, with a 50-pound limit per dog and a $75 cleaning fee per stay."
    if "breakfast" in topic or "desayuno" in topic or "dejeuner" in topic or "déjeuner" in topic:
        if spanish:
            return "El desayuno se sirve de 6:30 AM a 10:30 AM y solo está incluido cuando la tarifa lo indica."
        if french:
            return "Le petit déjeuner est servi de 6h30 à 10h30 et n'est inclus que lorsque le tarif choisi le précise."
        return "Breakfast is served from 6:30 AM to 10:30 AM and is included only when the selected rate says so."
    if "accessib" in topic or "accesib" in topic:
        if spanish:
            return "Las habitaciones accesibles pueden incluir duchas sin escalón, alarmas visuales y accesorios a baja altura. Solicite las características necesarias antes de reservar."
        if french:
            return "Les chambres accessibles peuvent inclure des douches de plain-pied, des alarmes visuelles et des équipements abaissés. Veuillez demander les caractéristiques nécessaires avant de réserver."
        return "Accessible rooms can include roll-in showers, visual alarms, and lowered fixtures. Please request the features you need before booking so availability can be confirmed."
    if spanish:
        return "Encontré la política de Aurora Hotel y puedo ayudarle con los detalles de su reserva."
    if french:
        return "J'ai trouvé la politique de l'hôtel Aurora et je peux vous aider avec les détails de votre réservation."
    return "I found the relevant Aurora Hotel policy and can help apply it to your reservation."


def make_provider(name: str | None = None):
    """Factory: returns MockProvider for PROVIDER=mock, else a live Provider."""
    name = (name or os.getenv("PROVIDER", "groq")).lower()
    if name == "mock":
        return MockProvider()
    return Provider(name)
