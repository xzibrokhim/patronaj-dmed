# Offline Digital Bridge · Lokal AI va patronaj demosi

Hamshira o‘zbekcha gapiradi, backend bemorning yoshi va sensorlarini beradi. Shu noutbukda tahlil yuradi, backend besh maydonli JSON oladi va hamshira javobni eshitadi. Ushbu versiya kattalar (18–120 yosh) uchun xakaton prototipi. Tibbiyot xodimi bemorni o‘zi baholaydi. Klinik aniqlik va bolalarga qo‘llash tekshirilmagan.

## 10-band planshet demosi

Windows’da `PATRONAJ-DEMO.cmd`ni oching. Brauzer [planshet ekranini](http://127.0.0.1:8002/) ochadi. Birinchi yuklashda **Planshet oflayn ochishga tayyor** yozuvi chiqishini kuting.

1. **Sinov aloqasi**ni o‘chiring. **Namuna**ni bosing va **Tashrifni saqlash**ni tanlang.
2. Sahifani qayta yuklang. Yozuvning **Navbatda** turganini ko‘ring.
3. **Sinov aloqasi**ni yoqing. **Qabul qilindi** chiqqach, **Qabul jurnali**ni oching. Jurnalda qurilmadagi yozuv bilan bir xil tashrif kodi ko‘rinadi.

Sinov uzgichi planshetdan qabul xizmatiga jo‘natishni pauza qiladi. Brauzerning Network → Offline rejimi oldindan yuklangan sahifani aloqa uzilganda qayta ochish va saqlashni tekshirish uchun ishlatiladi. Navbat brauzerning IndexedDB xotirasida saqlanadi. Sinov serveri 8003-portda qabul qiladi va yozuvlarni `.runtime/patronage-demo.sqlite3` fayliga yozadi.

Bir xil tashrifni qayta yuborish sinovi uchun `.venv\Scripts\python.exe -m pytest -q test_patronage_demo.py::test_retry_after_lost_receipt_preserves_original_receipt` buyrug‘ini bajaring. Sinov birinchi tasdiq yo‘qolganda qayta yuboradi va jurnalda bitta yozuv hamda birinchi qabul vaqti qolganini tekshiradi.

Lokal AI’ni ham ko‘rsatish uchun alohida **START.cmd**ni oching. Namuna bilan **AI tahlili**ni bosing. Planshetda matnli javob chiqadi. Karnay va mikrofon yo‘li quyida berilgan. Planshet demosida sintetik namunalarni ishlating.

## Windows noutbukida

1. `Offline-Digital-Bridge-Windows.zip` faylini noutbukdagi yozish mumkin bo‘lgan papkaga to‘liq chiqaring. Python, kutubxonalar, Ollama va uchala model paket ichida. Arxivning o‘zi va ochilgan fayllar uchun jami taxminan 15 GB bo‘sh joy ajrating.
2. `START.cmd`ni oching. Birinchi startda Ollama fayllari ochiladi va model xotiraga yuklanadi. Konsoldagi **Tayyor** yozuvidan keyin [holatni oching](http://localhost:8001/health). Ish vaqtida shu oynani ochiq qoldiring. Ctrl+C xizmatni to‘xtatadi.
3. Matnli demo uchun ayni papkada quyidagi buyruqni bajaring. Karnay qurilmasi Windows’da standart chiqish sifatida tanlangan bo‘lsin.

```powershell
curl.exe -i -H "Content-Type: application/json" --data-binary "@example.json" http://localhost:8001/analyze
```

4. Mikrofonli demo uchun `VOICE-DEMO.cmd`ni oching. Argumentsiz ishga tushganda yosh va datchiklar namunaviy bemorniki. Haqiqiy backenddan olingan JSONni ishlatish uchun `VOICE-DEMO.cmd visit.json` deb chaqiring. Enter bilan yozishni boshlang va tugating. Tanilgan matndagi **son, inkor va simptom**ni tekshiring. `+` tasdiqlaydi, yangi matn tuzatadi, bo‘sh Enter tashrifni bekor qiladi.

Safarga chiqishdan oldin Wi-Fi’ni uzib, serverni qayta ishga tushiring. Paketda ishlash uchun API kaliti yoki model yuklash talab qilinmaydi. Har bir yangi tashrifni bemor yozuviga bog‘lash va aloqa qaytganda yuborish Ibrohim backendining vazifasi.

## Backend shartnomasi

```python
import json
from pathlib import Path
from urllib.request import Request, urlopen

payload = Path("example.json").read_bytes()
request = Request("http://127.0.0.1:8001/analyze", data=payload,
                  headers={"Content-Type": "application/json"})
with urlopen(request, timeout=120) as response:
    result = json.load(response)
    print(result["risk_zone"], result["voice_reply_uz"])
    print(response.headers["X-Voice-Status"])
```

| Manzil | Qanday ishlatiladi |
| --- | --- |
| `POST /analyze` | `example.json` bilan bir xil uchta kirish maydoni. Javob — `risk_zone`, `risk_score`, `preliminary_diagnosis_uz`, `nurse_instructions_uz`, `voice_reply_uz`. Odatiy rejimda mahalliy karnaydan eshittirish tugagach HTTP javobi keladi. |
| `POST /analyze?speak=false` | Shu JSONni karnaysiz olish. |
| `POST /transcribe` | Body — mono 16 kHz PCM16 WAV baytlari, 0.34–30 soniya. Javob — `{"voice_text":"..."}`. Matn tekshirilgach yosh va sensorlar bilan `/analyze`ga yuboriladi. |
| `GET /health` | Mahalliy model va ovoz fayllari mavjudligini tekshiradi. Tayyor bo‘lsa 200, resurs yetishmasa 503 va `missing` ro‘yxati keladi. |

O‘pka audio holatlari `normal`, `wheezing_detected`, `crackles_detected` va `not_recorded`. Oxirgisi o‘pka tovushi yozilmaganini bildiradi va demo yashil zonasiga yetarli emas. Qurilma uzilgan yoki o‘lchov noma’lum bo‘lsa, backend normal qiymat qo‘shmasligi kerak. 422 javobda matn, yosh yoki o‘lchovni tekshiring. 409 — karnay oldingi bemorga xizmat qilyapti, keyinroq qayta uriniladi. 503 — lokal modelni yoki fayllarni tekshiring.

Karnay ishlamasa JSON saqlanadi va `X-Voice-Status: unavailable` keladi. Bunda hamshiraga yozma javobni ko‘rsating. `played` operatsion tizim audio buferni ijro etganini bildiradi. `X-Assessment-Source: urgent-sensor-fallback` — model bahosi olinmagan, lekin xavfli o‘lchov bo‘yicha mahalliy ogohlantirish berilgan. Aniq aytilgan hozirgi ko‘krak og‘rig‘ida model ishlamasa, xuddi shu tarzda `urgent-symptom-fallback` keladi. Modul tashqi chaqiruv yubormaydi.

## Model qanday baholaydi

Qwen3 4B Instruct, Q4_K_M, 2048 token kontekst. Model yosh, shikoyat va datchiklarni olib, hozirgi shikoyat turini hamda matndan dalilni ajratadi. Dastur dalil matnda borligini tekshiradi. O‘lchovlar model xavfni pasaytirib yuborishidan mustaqil tekshiriladi. Model vaznlari taxminan 2.5 GB. Faster-Whisper Small o‘zbekcha modeli CPU INT8’da va o‘zbekcha MMS ovozi ONNX Runtime CPU’da ishlaydi. Shu bilan videoxotira Ollama uchun ajratiladi. [Qwen modeli](https://ollama.com/library/qwen3:4b-instruct) · [STT vaznlari](https://huggingface.co/aslon1213/whisper-small-uz-with-uzbekvoice-ct2) · [ONNX ovoz](https://huggingface.co/davron112/mms-tts-uzb-cyrl-onnx)

Xavf ballari demo toifalarining kodlari — yashil 10, sariq 50, qizil 90. Bemorning kasallanish ehtimolini ifodalamaydi. Tizim ko‘rik uchun dastlabki xulosa beradi. Modelga dori yoki davolash dozalarini yozdirish qo‘llanmagan. Qizil ssenariyda hamshira shoshilinch yordam tartibini internetni kutmasdan boshlashi kerak.

Demo uchun shoshilinch sensor belgisi — SpO₂ 92 dan past, puls 60 dan past yoki 130 dan yuqori, nafas tezligi 10 dan past yoki 30 dan yuqori, harorat 36 dan past yoki 39 dan yuqori. Shikoyat va boshqa o‘lchovlar sariq yoki qizil zonaga olib kelishi mumkin. Yashil zona faqat aniq “shikoyati yo‘q” qaydi va demo mezoniga mos barcha sensorlar bilan beriladi. Bu chegaralar hamda 3 ball klinik validatsiyadan o‘tmagan. WHO IITT shoshilinch bo‘limda foydalanishga mo‘ljallangan. Uy tashrifi uchun soddalashtirilgan qoidalarni klinik ekspert alohida baholashi kerak. [WHO IITT jadvali](https://cdn.who.int/media/docs/default-source/integrated-health-services-%28ihs%29/csy/iitt/iitt_adult.pdf) · [WHO hayotiy ko‘rsatkichlar kartasi](https://cdn.who.int/media/docs/default-source/documents/emergency-care/reference-card-for-who-emergency-unit-form-general.pdf)

## Maqsadli qurilmada sinov

`START.cmd` ochiq paytda ayni papkada bajaring. Sinov sintetik bemor ma’lumotlaridan foydalanadi.

```powershell
.\python\python.exe -X utf8 verify_delivery.py
```

`demo-results` papkasiga har bir javobning zonasi, model yoki zaxira qoida ishlagani, karnay holati, API vaqti, kuzatilgan umumiy RAM/VRAM cho‘qqilari, namuna JSON va WAV yoziladi. Jonli hamshira ovozida sonlar va inkorlar, haqiqiy karnayda talaffuz va RTX 4050’dagi tezlik ham tekshirilishi kerak.

## Oflayn protokollar va DHP sinxronlash

`START.cmd` va `PATRONAJ-DEMO.cmd`ni oching. **Namuna** tugmasi bilan maydonlarni to‘ldiring va **AI tahlili** tugmasini bosing. Ochiq manbalardan saralangan WHO IITT va SSV protokoli bo‘limlari paketning ichida. Shikoyat yoki o‘lchovga mos bo‘lim Ollama’ga kontekst sifatida uzatiladi. Planshet manba havolalarini ko‘rsatadi. Backend bu havolalarni HTTP `Link` sarlavhasi orqali oladi. Havolani ochish uchun internet kerak, qidiruv va tahlil mahalliy ishlaydi. [WHO IITT](https://www.who.int/tools/triage) · [SSV klinik protokoli](https://gov.uz/oz/ssv/sections/view/46422)

DHP sinov muhiti uchun tashkilotchidan OAuth test klienti va oldindan ochilgan sintetik bemorning FHIR ID’sini oling. `DHP_TOKEN_URL`, `DHP_CLIENT_ID`, `DHP_CLIENT_SECRET` va `DHP_PATIENT_MAP` ni serverni ochadigan terminal muhitiga sozlang. Klientga scope berilgan bo‘lsa, `DHP_SCOPE`ni ham qo‘ying. `DHP_PATIENT_MAP` kodlarni FHIR Patient ID’lariga bog‘laydigan JSON faylning yo‘li. Masalan, test bemori `synthetic-104` bo‘lsa, fayl tarkibi `{"P-104":"synthetic-104"}`. Klient siri planshetga uzatilmaydi. [DHP API](https://dhp.uz/fhir/core/en/api-access.html) · [SSO hujjati](https://sso.dhp.uz/docs)

```powershell
.\PATRONAJ-DEMO.cmd --dhp-playground
```

DHP rejimi standart holda 8004-portda ochiladi va alohida navbat hamda qabul jurnalini ishlatadi. Shu rejimda yangi sintetik tashrif saqlang. Aloqa qaytganda backend shikoyatni `Encounter` ichida va mavjud sonli o‘lchovlarni `Observation` sifatida bitta FHIR tranzaksiyasida yuboradi. Takroriy so‘rovlar barqaror identifikatorlardan foydalanadi. Qabul faqat barcha resurslar tasdiqlanganda qayd qilinadi. AI xulosasi, tremor va o‘pka audio belgisi mahalliy tashrif yozuvida ko‘rinadi. [UZ Core hayotiy ko‘rsatkichlar](https://dhp.uz/fhir/core/en/vital-signs.html)

DHP playground’da shartli yaratish qo‘llanmaydi. Tranzaksiya natijasi yo‘qolsa, adapter saqlangan identifikatorlar bo‘yicha qabulni tekshiradi. Yozuvlar topilmaguncha tashrif navbatda qoladi. Serverga ulanib bo‘lmagan so‘rov aloqa tiklangach yana yuboriladi. Tashkilotchi bergan klient ruxsati va sintetik bemor bilan jonli qabul hali tekshiriladi. DMED mahsulotining maxsus API’si uchun tashkilotchining shartnomasi kerak. [DHP playground imkoniyatlari](https://playground.dhp.uz/fhir/metadata)

Meta MMS o‘zbekcha ovozi CC BY-NC 4.0 bilan chiqarilgan. Ushbu paket lokal xakaton namoyishiga tayyorlangan. [Meta model kartasi](https://huggingface.co/facebook/mms-tts-uzb-script_cyrillic) · [Litsenziya](https://creativecommons.org/licenses/by-nc/4.0/)

## Manba kodidan qayta yig‘ish

Windows x64 va `uv` mavjud dasturchi kompyuterida bir marta internet bilan `.\prepare.ps1`ni bajaring. U loyiha ichiga Python 3.12 muhiti, Ollama va tanlangan vaznlarni joylaydi. Keyin `START.cmd` ishlaydi. `.venv\Scripts\python.exe build_delivery.py` ko‘chirish uchun ZIPni `dist` papkasiga yozadi. Portativ ZIPdan foydalanayotgan Ibrohim uchun bu bosqich kerak emas.
