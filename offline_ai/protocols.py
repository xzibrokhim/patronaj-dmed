"""Small, bundled adult protocol corpus. Summaries and links are curated in code."""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Passage:
    title: str
    url: str
    summary: str
    patterns: tuple[str, ...]


IITT = "https://cdn.who.int/media/docs/default-source/integrated-health-services-%28ihs%29/csy/iitt/iitt_adult.pdf"
PNEUMONIA = "https://api-portal.gov.uz/uploads/10/2026/03/09/cf56f310-1266-d7c0-3ed7-caaa1bf30866_media_.pdf"
CORPUS = (
    Passage("WHO IITT · xavfli hayotiy ko‘rsatkichlar", IITT,
            "Emergency-unit guidance: high-risk vital signs require higher triage or immediate supervising clinician review.",
            (r"\bspo2\b", r"\bsaturats", r"\bpuls\b", r"\bтахикард")),
    Passage("WHO IITT · nafas olish va shoshilinch belgilar", IITT,
            "Emergency-unit guidance: assess respiratory distress and central cyanosis as high-priority signs.",
            (r"\bnafas", r"\bнафас", r"\bhansira", r"\bҳансира", r"\bko'kr", r"\bкўкр")),
    Passage("O‘zbekiston SSV · pnevmoniya protokoli, 3.3-bo‘lim", PNEUMONIA,
            "Adult community-acquired pneumonia protocol, physical examination: pulse oximetry helps assess hypoxemia and respiratory impairment.",
            (r"\bpnevmon", r"\bпневмон", r"\bzotiljam", r"\bзотилжам")),
)


def retrieve(visit):
    text = re.sub("[ʻ‘’ʼ`]", "'", visit.voice_text.casefold())
    # Use only the measured value, with a scope matching the source.
    if visit.sensors.spo2_percent < 92:
        text += " spo2"
    respiratory = any(re.search(pattern, text) for pattern in CORPUS[1].patterns[:4])
    if respiratory and visit.sensors.temperature_c >= 38 and visit.sensors.spo2_percent < 92:
        text += " pnevmoniya"
    matches = []
    for passage in CORPUS:
        score = sum(bool(re.search(pattern, text)) for pattern in passage.patterns)
        if score:
            matches.append((score, passage))
    return tuple(passage for _, passage in sorted(matches, key=lambda item: -item[0])[:3])
