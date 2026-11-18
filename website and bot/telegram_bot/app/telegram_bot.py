from __future__ import annotations

import asyncio
import os
import secrets
import hmac

import httpx
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

from .main import BASE_DIR, get_doctor_user_id, init_db, register_doctor_user

load_dotenv(BASE_DIR / ".env")
router = Router()
BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
INTERNAL_KEY = os.getenv("INTERNAL_API_KEY", "").strip()
PAIRING_CODE = secrets.token_urlsafe(9)


class DoctorOnlyMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: CallbackQuery, data):
        if event.from_user.id != get_doctor_user_id():
            await event.answer("Bu amal faqat ulangan shifokor uchun.", show_alert=True)
            return
        return await handler(event, data)


router.callback_query.middleware(DoctorOnlyMiddleware())


async def post_action(patient_id: int, action: str, message: str) -> bool:
    if not INTERNAL_KEY:
        return False
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.post(
                f"{BACKEND_URL}/api/internal/doctor-actions",
                headers={"X-Internal-Key": INTERNAL_KEY},
                json={"patient_id": patient_id, "action": action, "message": message},
            )
            response.raise_for_status()
        return True
    except Exception:
        return False


@router.message(CommandStart())
async def start(message: Message, command: CommandObject) -> None:
    if not message.from_user or message.chat.type != "private":
        await message.answer("Botga shaxsiy xabarda /start yuboring.")
        return
    user_id = message.from_user.id
    registered = get_doctor_user_id()
    if registered is None:
        if not command.args or not hmac.compare_digest(command.args, PAIRING_CODE):
            await message.answer(
                f"Sizning Telegram user ID: {user_id}\n"
                "Ulanish uchun loyiha egasi bergan /start KOD buyrug‘ini yuboring."
            )
            return
        register_doctor_user(user_id)
    elif registered != user_id:
        await message.answer(f"Sizning user ID: {user_id}. Bot boshqa shifokorga ulangan.")
        return
    await message.answer(
        "<b>Digital Bridge · Tuman shifokori</b>\n\n"
        f"User ID orqali ulandi: <code>{user_id}</code>\n"
        "Qishloq planshetlari sinxronlanganda yangi bemor kartalari shu yerga keladi.\n\n"
        "Bu hackathon prototipi. Faqat ma’lumot uchun; professional tibbiy maslahat o‘rnini bosmaydi. "
        "Tez yordam tugmasi faqat ehtiyojni qayd etadi; haqiqiy dispetcherlik tizimiga ulanmagan.",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("confirm:"))
async def confirm(callback: CallbackQuery) -> None:
    patient_id = int(callback.data.split(":", 1)[1])
    ok = await post_action(patient_id, "confirmed", "Shifokor ma’lumotni ko‘rdi va qabul qildi.")
    await callback.answer("Planshetga yuborildi" if ok else "Backend bilan aloqa yo‘q", show_alert=not ok)
    if ok and callback.message:
        await callback.message.answer(f"Bemor #{patient_id}: yozuv qabul qilinganligi tasdiqlandi.")


@router.callback_query(F.data.startswith("ambulance:"))
async def ambulance(callback: CallbackQuery) -> None:
    patient_id = int(callback.data.split(":", 1)[1])
    text = (
        "Tez yordam zarurligini tekshirish so‘raldi. "
        "DEMO: bu haqiqiy tez yordam dispetcheriga yuborilmadi; telefon orqali tasdiqlang."
    )
    ok = await post_action(patient_id, "ambulance_requested", text)
    await callback.answer(
        "SO‘ROV qayd etildi — bu dispatch emas" if ok else "Backend bilan aloqa yo‘q",
        show_alert=True,
    )
    if ok and callback.message:
        await callback.message.answer(
            f"Bemor #{patient_id}: tez yordam ehtiyoji bo‘yicha so‘rov qayd etildi.\n"
            "Haqiqiy dispatch integratsiyasi yo‘q. TODO: tashkilotning tasdiqlangan eskalatsiya tartibini ulang."
        )


@router.callback_query(F.data.startswith("reply:"))
async def reply_menu(callback: CallbackQuery) -> None:
    patient_id = int(callback.data.split(":", 1)[1])
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Yozuv qabul qilindi", callback_data=f"template:received:{patient_id}")],
            [InlineKeyboardButton(text="Tasdiqlangan kanal orqali bog‘laning", callback_data=f"template:contact:{patient_id}")],
            [InlineKeyboardButton(text="TODO: klinik javob shabloni", callback_data=f"template:todo:{patient_id}")],
        ]
    )
    await callback.answer()
    if callback.message:
        await callback.message.answer(f"Bemor #{patient_id} uchun javobni tanlang:", reply_markup=keyboard)


TEMPLATES = {
    "received": "Yozuv qabul qilindi. Klinik ko‘rib chiqish holati: TODO.",
    "contact": "Tashkilot tasdiqlagan aloqa kanali orqali tuman shifokori bilan bog‘laning. TODO: haqiqiy aloqa ma’lumotini kiriting.",
    "todo": "TODO: ushbu javob shablonini vakolatli klinik mutaxassis tasdiqlashi kerak.",
}


@router.callback_query(F.data.startswith("template:"))
async def template_reply(callback: CallbackQuery) -> None:
    _, code, raw_id = callback.data.split(":", 2)
    patient_id = int(raw_id)
    message = TEMPLATES.get(code, "TODO: tasdiqlangan javob shabloni topilmadi.")
    ok = await post_action(patient_id, "doctor_reply", message)
    await callback.answer("Javob planshetga yuborildi" if ok else "Backend bilan aloqa yo‘q", show_alert=not ok)
    if ok and callback.message:
        await callback.message.edit_text(f"Bemor #{patient_id} uchun yuborildi:\n\n{message}")


async def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is empty. Add it to .env first.")
    if not INTERNAL_KEY:
        raise SystemExit("INTERNAL_API_KEY is empty. Add a long random value to .env first.")
    bot = Bot(token=token)
    init_db()
    try:
        identity = await bot.get_me()
        print(f"Bot @{identity.username} connected; polling is starting.", flush=True)
        if get_doctor_user_id() is None:
            print(f"Doctor setup: https://t.me/{identity.username}?start={PAIRING_CODE}", flush=True)
        dispatcher = Dispatcher()
        dispatcher.include_router(router)
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
