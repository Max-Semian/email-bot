"""
Telegram Email-Bot
Commands:
  /start  — welcome message
  /send   — start mailing flow
  /cancel — cancel current operation
"""

import asyncio
import logging
import os

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import mailer

load_dotenv()
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def _admin_ids() -> set[int]:
    """Return allowed Telegram user IDs from ADMIN_IDS env var (comma-separated)."""
    raw = os.getenv("ADMIN_IDS", "")
    if not raw.strip():
        return set()  # empty = no restriction
    return {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()}


def admin_only(func):
    """Decorator: reject non-admin users when ADMIN_IDS is set."""
    import functools

    @functools.wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        admins = _admin_ids()
        user_id = (
            update.effective_user.id if update.effective_user else None
        )
        if admins and user_id not in admins:
            if update.message:
                await update.message.reply_text("⛔ Доступ запрещён.")
            elif update.callback_query:
                await update.callback_query.answer("⛔ Доступ запрещён.", show_alert=True)
            return ConversationHandler.END
        return await func(update, ctx)

    return wrapper


# Conversation states
SUBJECT, ADDRESSES, TEMPLATE, CONFIRM = range(4)

# Keys in user_data
KEY_SUBJECT = "subject"
KEY_ADDRESSES = "addresses"     # list[dict]
KEY_TEMPLATE = "template_str"  # str


# ─── Helpers ────────────────────────────────────────────────────────────────

def _gmail_cfg() -> tuple[str, str, str]:
    user = os.getenv("GMAIL_USER", "")
    pwd = os.getenv("GMAIL_APP_PASSWORD", "")
    name = os.getenv("SENDER_NAME", "")
    return user, pwd, name


def _delay() -> float:
    return float(os.getenv("EMAIL_DELAY", "1.0"))


# ─── Handlers ───────────────────────────────────────────────────────────────

@admin_only
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я бот для email-рассылки через Gmail.\n\n"
        "Команды:\n"
        "/send — начать рассылку\n"
        "/cancel — отменить"
    )


@admin_only
async def cmd_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введите *тему письма*:", parse_mode=ParseMode.MARKDOWN)
    return SUBJECT


async def got_subject(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    subject = update.message.text.strip()
    if mailer.contains_cyrillic(subject):
        await update.message.reply_text(
            "Тема письма содержит кириллицу. Введите тему на английском, например: Bass2Face Promo Code"
        )
        return SUBJECT

    ctx.user_data[KEY_SUBJECT] = subject
    await update.message.reply_text(
        "Отправьте файл с адресами получателей (.txt).\n"
        "Формат: `адрес@example.com;Имя` — по одному на строку, или через `;`",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ADDRESSES


async def got_addresses(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc:
        await update.message.reply_text("Пожалуйста, отправьте файл .txt с адресами.")
        return ADDRESSES

    file = await doc.get_file()
    raw_bytes = await file.download_as_bytearray()
    text = raw_bytes.decode("utf-8", errors="replace")

    recipients = mailer.parse_addresses(text)
    if not recipients:
        await update.message.reply_text("Не найдено ни одного email-адреса. Проверьте формат файла.")
        return ADDRESSES

    ctx.user_data[KEY_ADDRESSES] = recipients
    await update.message.reply_text(
        f"Найдено адресов: *{len(recipients)}*\n\nТеперь отправьте HTML-шаблон письма.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return TEMPLATE


async def got_template(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc:
        await update.message.reply_text("Пожалуйста, отправьте HTML-файл шаблона.")
        return TEMPLATE

    file = await doc.get_file()
    raw_bytes = await file.download_as_bytearray()
    raw_template = raw_bytes.decode("utf-8", errors="replace")
    try:
        template_str, warnings = mailer.normalize_template(raw_template)
    except mailer.TemplateError as e:
        await update.message.reply_text(
            "Шаблон не подходит для рассылки:\n"
            f"{e}\n\n"
            "Нужно отправить чистый HTML-файл. Не используйте сохранение страницы "
            "как Webpage Complete / .mht / .mhtml; картинки должны быть по https:// URL."
        )
        return TEMPLATE

    ctx.user_data[KEY_TEMPLATE] = template_str

    recipients = ctx.user_data[KEY_ADDRESSES]
    subject = ctx.user_data[KEY_SUBJECT]
    sender_email, _, _ = _gmail_cfg()

    preview_lines = [f"`{r['email']}`" + (f" ({r['name']})" if r["name"] else "") for r in recipients[:5]]
    preview = "\n".join(preview_lines)
    if len(recipients) > 5:
        preview += f"\n_...и ещё {len(recipients) - 5}_"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Начать рассылку", callback_data="confirm"),
            InlineKeyboardButton("❌ Отмена", callback_data="cancel"),
        ]
    ])
    warning_text = ""
    if warnings:
        warning_text = "\n\n⚠️ " + "\n".join(warnings)

    await update.message.reply_text(
        f"*Подтверждение рассылки*\n\n"
        f"📧 Отправитель: `{sender_email}`\n"
        f"📝 Тема: *{subject}*\n"
        f"👥 Получателей: *{len(recipients)}*\n\n"
        f"{preview}"
        f"{warning_text}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )
    return CONFIRM


async def got_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        await query.edit_message_text("Рассылка отменена.")
        ctx.user_data.clear()
        return ConversationHandler.END

    recipients = ctx.user_data[KEY_ADDRESSES]
    template_str = ctx.user_data[KEY_TEMPLATE]
    subject = ctx.user_data[KEY_SUBJECT]
    sender_email, app_password, sender_name = _gmail_cfg()

    if not sender_email or not app_password:
        await query.edit_message_text(
            "Ошибка: не настроены GMAIL_USER и GMAIL_APP_PASSWORD в .env"
        )
        return ConversationHandler.END

    progress_msg = await query.edit_message_text(
        f"⏳ Начинаю рассылку для {len(recipients)} получателей..."
    )

    sent = failed = 0
    errors: list[str] = []
    accepted_ids: list[str] = []
    update_every = max(1, len(recipients) // 10)  # update progress ~10 times

    # Run blocking IO in a thread pool to not block the event loop
    loop = asyncio.get_event_loop()

    def do_send():
        nonlocal sent, failed
        results = []
        gen = mailer.send_all(
            recipients, template_str, subject,
            sender_email, app_password, sender_name, _delay()
        )
        for result in gen:
            results.append(result)
        return results

    try:
        results = await loop.run_in_executor(None, do_send)
    except Exception as e:
        error_msg = str(e)
        if "Authentication" in error_msg or "Username and Password" in error_msg:
            error_msg = (
                "Ошибка аутентификации Gmail.\n"
                "Убедитесь, что используете App Password, а не обычный пароль.\n"
                "https://myaccount.google.com/apppasswords"
            )
        await progress_msg.edit_text(f"❌ Ошибка подключения к Gmail:\n{error_msg}")
        ctx.user_data.clear()
        return ConversationHandler.END

    for r in results:
        if r["ok"]:
            sent += 1
            if r.get("message_id"):
                accepted_ids.append(r["message_id"])
        else:
            failed += 1
            logger.warning("Email delivery failed for %s: %s", r["email"], r.get("error", "?"))
            errors.append(f"`{r['email']}`: {r.get('error', '?')}")
        # Update progress periodically
        idx = r["index"]
        if idx % update_every == 0 or idx == len(recipients):
            pct = int(idx / len(recipients) * 100)
            try:
                await progress_msg.edit_text(
                    f"⏳ Отправка... {idx}/{len(recipients)} ({pct}%)\n"
                    f"✅ {sent}  ❌ {failed}"
                )
            except Exception:
                pass  # ignore "message not modified" errors

    summary = f"✅ Рассылка завершена!\n\nПринято Gmail SMTP: *{sent}*\nОшибок SMTP: *{failed}*"
    if accepted_ids:
        summary += f"\n\nMessage-ID последнего письма: `{accepted_ids[-1]}`"
        summary += "\nЕсли после этого пришёл bounce/undelivered, пришлите текст bounce — это уже отказ после принятия SMTP."
    if errors:
        error_list = "\n".join(errors[:10])
        if len(errors) > 10:
            error_list += f"\n_...и ещё {len(errors) - 10} ошибок_"
        summary += f"\n\n*Ошибки:*\n{error_list}"

    await progress_msg.edit_text(summary, parse_mode=ParseMode.MARKDOWN)
    ctx.user_data.clear()
    return ConversationHandler.END


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("Операция отменена.")
    return ConversationHandler.END


async def fallback_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Используйте /send для запуска рассылки или /cancel для отмены.")


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в .env")

    app = Application.builder().token(token).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("send", cmd_send)],
        states={
            SUBJECT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, got_subject)],
            ADDRESSES: [MessageHandler(filters.Document.ALL, got_addresses)],
            TEMPLATE:  [MessageHandler(filters.Document.ALL, got_template)],
            CONFIRM:   [CallbackQueryHandler(got_confirm)],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(filters.ALL, fallback_text),
        ],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(conv)

    logger.info("Bot started. Listening for updates...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
