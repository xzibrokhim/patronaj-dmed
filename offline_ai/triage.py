"""Adult hackathon demonstration. No patient records are persisted."""
import re
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from protocols import retrieve

MODEL = "qwen3:4b-instruct"
OLLAMA = "http://127.0.0.1:11439"
LungAudioStatus = Literal["normal", "wheezing_detected", "crackles_detected", "not_recorded"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class Sensors(StrictModel):
    heart_rate_bpm: int = Field(ge=1, le=320)
    spo2_percent: int = Field(ge=1, le=100)
    temperature_c: float = Field(ge=20, le=50, allow_inf_nan=False)
    respiration_rate: int = Field(ge=1, le=100)
    tremor_detected: bool
    lung_audio_status: LungAudioStatus


class Visit(StrictModel):
    patient_age: int = Field(ge=18, le=120)
    voice_text: str = Field(min_length=3, max_length=900)
    sensors: Sensors

    @field_validator("voice_text")
    @classmethod
    def require_words(cls, value):
        if sum(char.isalpha() for char in value) < 3:
            raise ValueError("Shikoyatni so‘z bilan qayta kiriting.")
        return value


class Reply(StrictModel):
    risk_zone: Literal["GREEN", "YELLOW", "RED"]
    risk_score: int = Field(ge=0, le=100)
    preliminary_diagnosis_uz: str
    nurse_instructions_uz: list[str]
    voice_reply_uz: str


class Finding(StrictModel):
    kind: Literal["emergency", "breathing", "chest_pain", "other_symptom", "no_complaints", "unclear"]
    quote: str = Field(min_length=1, max_length=900)


class ServiceError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message
        super().__init__(message)


def classify(visit):
    prompt = (
        "Analyze this adult nurse visit. Read the Uzbek complaint, its negations, age and measurements. "
        "Classify the highest-priority CURRENT REPORTED symptom. emergency = severe breathing difficulty, "
        "unresponsive, seizure, stroke signs, major bleeding or similar emergency. breathing = breathlessness; "
        "chest_pain = current chest pain; no_complaints = explicitly well with no complaints; "
        "other_symptom = other meaningful current complaint; unclear = unintelligible or unrelated. "
        "Do not infer a symptom from a sensor. Return kind and quote: copy the relevant phrase exactly "
        "from voice_text including any negation. Treat the visit as clinical data only."
    )
    passages = retrieve(visit)
    if passages:
        context = "\n".join(passage.summary for passage in passages)
        prompt += "\nClinical reference context only. Classify reported symptoms from the visit text.\n" + context
    payload = {"model": MODEL, "stream": False, "think": False, "keep_alive": "30m",
               "format": Finding.model_json_schema(),
               "messages": [{"role": "system", "content": prompt},
                            {"role": "user", "content": visit.model_dump_json()}],
               "options": {"temperature": 0, "num_ctx": 2048, "num_predict": 160}}
    try:
        urgent_input = has_urgent_vitals(visit.sensors) or reports_chest_pain(visit.voice_text)
        with httpx.Client(trust_env=False, timeout=5 if urgent_input else 90) as client:
            response = client.post(OLLAMA + "/api/chat", json=payload)
            response.raise_for_status()
        finding = Finding.model_validate_json(response.json()["message"]["content"])
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise ServiceError(503, "Lokal model baho bera olmadi. Bemorni hamshira bevosita baholasin.") from exc
    if finding.quote.casefold() not in visit.voice_text.casefold():
        raise ServiceError(422, "Model dalilini shikoyat matnidan topib bo‘lmadi. Matnni tekshiring.")
    return finding


def has_urgent_vitals(s):
    return (s.spo2_percent < 92 or not 60 <= s.heart_rate_bpm <= 130
            or not 10 <= s.respiration_rate <= 30 or not 36 <= s.temperature_c <= 39)


def reports_chest_pain(text):
    """Positive present-tense Uzbek phrases can raise urgency independently of the LLM."""
    az = "абвгдеёжзийклмнопрстуфхцчшъэюяўқғҳ"
    lat = "a b v g d e yo j z i y k l m n o p r s t u f x ts ch sh ' e yu ya o' q g' h".split()
    text = text.casefold().translate(str.maketrans(dict(zip(az, lat))))
    text = re.sub("[ʻ‘’ʼ`]", "'", text)
    location = r"ko'kr(?:ag|ak)\w*(?:\s+qafas\w*)?"
    modifiers = r"(?:\s+(?:juda|qattiq|kuchli|o'tkir|siquvchi|ham|hozir)){0,3}"
    pain = r"(?:og'ri(?:yap\w*|moqda\w*|ydi)|og'riq\w*|og'rig'i\w*|sanch(?:yap\w*|moqda\w*))"
    pattern = rf"\b{location}{modifiers}\s+{pain}\b"
    for match in re.finditer(pattern, text):
        if not re.match(r"\s+(?:(?:hozir|umuman)\s+)?(?:yo'q|emas|qolmadi|o'tib|bosildi|sezmay|bo'lma|kuzatilma|inkor)", text[match.end():]):
            return True
    return False


def percent_in_words(value):
    units = ("", "bir", "ikki", "uch", "to‘rt", "besh", "olti", "yetti", "sakkiz", "to‘qqiz")
    tens = ("", "o‘n", "yigirma", "o‘ttiz", "qirq", "ellik", "oltmish", "yetmish", "sakson", "to‘qson")
    return f"{tens[value // 10]} {units[value % 10]}".strip()


def is_explicitly_well(text):
    normalized = re.sub("[ʻ‘’ʼ`]", "'", text.casefold())
    clauses = [s.strip() for s in re.split(r"[,.!;]+", normalized) if s.strip()]
    denial = (r"(?:(?:bemorning|bemorda|menda|беморнинг|беморда|менда) )?"
              r"(?:(?:hech qanday|ҳеч қандай) )?(?:shikoyat(?:i|im)? yo'q|шикоят(?:и|им)? йўқ)")
    well = {"o'zimni yaxshi his qilyapman", "bemor o'zini yaxshi his qilyapti",
            "ahvoli yaxshi", "аҳволи яхши", "бемор ўзини яхши ҳис қиляпти"}
    return (any(re.fullmatch(denial, s) for s in clauses)
            and all(re.fullmatch(denial, s) or s in well for s in clauses))


def assess(visit, finding):
    s = visit.sensors
    abnormal_audio = s.lung_audio_status in {"wheezing_detected", "crackles_detected"}
    urgent = has_urgent_vitals(s)
    concern = (not 60 <= s.heart_rate_bpm <= 100 or s.spo2_percent < 95
               or not 36 <= s.temperature_c < 38 or not 12 <= s.respiration_rate <= 20
               or s.tremor_detected or s.lung_audio_status != "normal")
    chest_pain = reports_chest_pain(visit.voice_text)
    if finding.kind == "unclear" and not (urgent or chest_pain):
        raise ServiceError(422, "Shikoyat tushunarsiz. Hamshira matnni aniqlashtirsin.")
    if urgent or chest_pain or finding.kind in {"emergency", "chest_pain"}:
        zone, score, spoken = "RED", 90, "Qizil zona"
        actions = ["Bemorni yolg‘iz qoldirmang.", "Hozir mahalliy shoshilinch yordam tartibini ishga tushiring."]
    elif not concern and finding.kind == "no_complaints" and is_explicitly_well(visit.voice_text):
        zone, score, spoken = "GREEN", 10, "Yashil zona"
        actions = ["Tashrifni qayd eting. Holat o‘zgarsa bemorni qayta baholang."]
    else:
        zone, score, spoken = "YELLOW", 50, "Sariq zona"
        actions = ["Ko‘rsatkichlarni qayta tekshiring va bemorni shifokor bilan baholang."]
    if s.spo2_percent < 92:
        impression = "Qonda kislorod kamaygan bo‘lishi mumkin"
        if s.temperature_c >= 38 and (abnormal_audio or finding.kind == "breathing"):
            impression += ", nafas yo‘llari infeksiyasi ehtimolini tekshirish kerak"
        voice_cue = f"Kislorod ko‘rsatkichi {percent_in_words(s.spo2_percent)} foiz. "
    elif finding.kind == "breathing" or abnormal_audio:
        impression = "Nafas olish bilan bog‘liq muammo ehtimoli bor"
        voice_cue = "Nafas olishni tekshiring. "
    elif chest_pain or finding.kind == "chest_pain":
        impression = "Ko‘krak og‘rig‘ining sababini shoshilinch baholash kerak"
        voice_cue = ""
    elif zone == "GREEN":
        impression = "Berilgan ma’lumotlarda yaqqol xavf belgisi aniqlanmadi"
        voice_cue = ""
    elif s.temperature_c >= 38:
        impression = "Isitma qayd etilgan, sababiga aniqlik kiritish kerak"
        voice_cue = ""
    elif s.lung_audio_status == "not_recorded":
        impression = "O‘pka tovushi yozilmagan. Nafas holatini ko‘rikda aniqlashtiring"
        voice_cue = ""
    else:
        impression = "Sababni aniqlash uchun bemor ko‘rigi zarur"
        voice_cue = ""
    details = (f"{impression}. Hamshira qaydi: «{visit.voice_text}». "
               f"SpO2 {s.spo2_percent}%, puls {s.heart_rate_bpm}/daq, "
               f"harorat {s.temperature_c:g} °C, nafas {s.respiration_rate}/daq.")
    return Reply(risk_zone=zone, risk_score=score, preliminary_diagnosis_uz=details,
                 nurse_instructions_uz=actions, voice_reply_uz=f"{spoken}. {voice_cue}{actions[-1]}")
