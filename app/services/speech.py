"""Speech controls are a capability gate only (M8.2 part D; ADR-124 item 10; AC-M8.6).

The practice view shows a disabled "Dictate" or "Read aloud" button when the provider in use
declares the matching flag. Nothing records, transcribes or speaks: there is no speech route and
no provider speech method.
"""

from dataclasses import dataclass

from app.modules.ai import AICapabilities


@dataclass(frozen=True)
class SpeechControls:
    dictate: bool = False
    read_aloud: bool = False


def speech_controls(capabilities: AICapabilities) -> SpeechControls:
    return SpeechControls(
        dictate=capabilities.speech_to_text, read_aloud=capabilities.text_to_speech
    )
