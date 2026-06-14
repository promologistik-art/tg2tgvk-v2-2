import logging
import re
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from sqlalchemy import select, delete
from database import AsyncSessionLocal
from models import TargetChannel, Project
from config import Config
from .utils import require_project, get_sources_count, get_project_target, send_project_ready_message, check_user_access
from .constants import AWAITING_VK_TOKEN, AWAITING_VK_GROUP, CURRENT_PROJECT_KEY

logger = logging.getLogger(__name__)


async def _exchange_token(short_token: str) -> str:
    """Обменивает краткосрочный пользовательский токен на вечный."""
    url = "https://oauth.vk.com/access_token"
    params = {
        "client_id": Config.VK_CLIENT_ID,
        "client_secret": Config.VK_CLIENT_SECRET,
        "v": Config.VK_API_VERSION,
        "access_token": short_token,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as resp:
                result = await resp.json()
                logger.info(f"Token exchange result: expires_in={result.get('expires_in', '?')}")
                if "access_token" in result:
                    return result["access_token"]
                else:
                    logger.error(f"Token exchange failed: {result}")
                    # Если обмен не сработал — пробуем вернуть исходный
                    return short_token
    except Exception as e:
        logger.error(f"Token exchange error: {e}")
        return short_token


# ============ ДОБАВЛЕНИЕ VK-ЦЕЛИ ============

async def add_target_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_project = context.user_data.get(CURRENT_PROJECT_KEY)
    context.user_data.clear()
    if current_project:
        context.user_data[CURRENT_PROJECT_KEY] = current_project
    
    telegram_id = update.effective_user.id
    
    has_access, message, user = await check_user_access(telegram_id)
    if not has_access:
        await update.message.reply_text(message)
        return ConversationHandler.END
    
    project = await require_project(update, context)
    if not project:
        return ConversationHandler.END
    
    target = await get_project_target(project.id)
    if target:
        target_name = target.vk_group_name or target.channel_title or "цель"
        await update.message.reply_text(
            f"⚠️ В текущем проекте «{project.name}» уже есть цель:\n"
            f"📤 {target_name}\n\n"
            f"Чтобы добавить новую, сначала удалите текущую через /my_targets\n"
            f"Или переключитесь на другой проект в /my_projects"
        )
        return ConversationHandler.END
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Project).where(
                Project.user_id == telegram_id,
                Project.id != project.id,
                Project.is_active == True
            )
        )
        other_projects = result.scalars().all()
    
    other_with_target = []
    for p in other_projects:
        t = await get_project_target(p.id)
        if t:
            other_with_target.append((p, t))
    
    if other_with_target:
        text = f"📁 Текущий проект: «{project.name}»\n\n"
        text += "ℹ️ Цель уже существует в других проектах:\n"
        for p, t in other_with_target:
            t_name = t.vk_group_name or t.channel_title or "цель"
            text += f"• Проект «{p.name}» → {t_name}\n"
        text += f"\nДобавляем новую VK-цель в проект «{project.name}». Продолжить?"
        
        keyboard = []
        for p, t in other_with_target:
            keyboard.append([InlineKeyboardButton(
                f"🔄 Переключиться на «{p.name}»",
                callback_data=f"select_project_{p.id}"
            )])
        keyboard.append([InlineKeyboardButton(
            f"✅ Да, добавить в «{project.name}»",
            callback_data="add_target_continue"
        )])
        
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return ConversationHandler.END
    
    return await _show_token_prompt(update, context, project)


async def add_target_continue_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    project = await require_project(update, context)
    if not project:
        return ConversationHandler.END
    
    target = await get_project_target(project.id)
    if target:
        target_name = target.vk_group_name or target.channel_title or "цель"
        await query.edit_message_text(
            f"⚠️ В проекте «{project.name}» уже есть цель: {target_name}\n"
            f"Удалите её через /my_targets"
        )
        return ConversationHandler.END
    
    return await _show_token_prompt(query, context, project)


async def _show_token_prompt(target, context: ContextTypes.DEFAULT_TYPE, project: Project):
    context.user_data['temp_project_id'] = project.id
    context.user_data['temp_project_name'] = project.name
    
    text = (
        f"📤 <b>Добавление VK-цели в «{project.name}»</b>\n\n"
        f"<b>🔑 Шаг 1 из 2: Получите VK токен</b>\n\n"
        f"1. Перейдите по ссылке и нажмите «Разрешить»:\n"
        f"<a href='{Config.VK_AUTH_URL}'>🔗 Получить токен VK</a>\n\n"
        f"2. После авторизации вы попадёте на страницу blank.html\n"
        f"3. Скопируйте <b>всю строку</b> из адресной строки браузера\n"
        f"   (начинается с <code>vk1.a.</code>...)\n"
        f"4. Отправьте скопированный токен сюда\n\n"
        f"<i>Бот обменяет его на вечный токен автоматически.</i>\n\n"
        f"/cancel — отмена"
    )
    
    if hasattr(target, 'edit_message_text'):
        await target.edit_message_text(text, parse_mode="HTML", disable_web_page_preview=True)
    else:
        await target.message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)
    
    return AWAITING_VK_TOKEN


async def add_target_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token = update.message.text.strip()
    
    if not token.startswith("vk1.a.") and len(token) < 50:
        await update.message.reply_text(
            "❌ Не похоже на VK токен. Токен должен начинаться с <code>vk1.a.</code>\n\n"
            "Скопируйте всю строку из адресной строки браузера после авторизации.\n"
            "/cancel — отмена",
            parse_mode="HTML"
        )
        return AWAITING_VK_TOKEN
    
    # Обмениваем краткосрочный токен на вечный
    msg = await update.message.reply_text("🔄 Обмениваю токен на вечный...")
    
    eternal_token = await _exchange_token(token)
    
    await msg.delete()
    
    if not eternal_token:
        await update.message.reply_text(
            "❌ Не удалось обменять токен на вечный.\n"
            "Попробуйте получить новый токен по ссылке и отправьте его.\n"
            "/cancel — отмена"
        )
        return AWAITING_VK_TOKEN
    
    # Проверяем вечный токен
    try:
        async with aiohttp.ClientSession() as session:
            params = {"access_token": eternal_token, "v": Config.VK_API_VERSION}
            async with session.get("https://api.vk.com/method/users.get", params=params) as resp:
                result = await resp.json()
    except Exception as e:
        logger.error(f"VK token check failed: {e}")
        await update.message.reply_text(
            "❌ Не удалось проверить токен. Попробуйте снова.\n/cancel — отмена"
        )
        return AWAITING_VK_TOKEN
    
    if "error" in result:
        await update.message.reply_text(
            f"❌ Токен недействителен: {result['error'].get('error_msg', 'ошибка')}\n"
            "Попробуйте получить новый токен.\n/cancel — отмена"
        )
        return AWAITING_VK_TOKEN
    
    context.user_data['temp_vk_token'] = eternal_token
    
    await update.message.reply_text(
        f"✅ Токен получен и обменян на вечный!\n\n"
        f"<b>🔗 Шаг 2 из 2: Отправьте ссылку на группу VK</b>\n\n"
        f"Например:\n"
        f"• <code>https://vk.com/club123456</code>\n"
        f"• <code>vk.com/public123456</code>\n"
        f"• <code>https://vk.com/moyagruppa</code>\n\n"
        f"<i>Вы должны быть администратором этой группы.</i>\n\n"
        f"/cancel — отмена",
        parse_mode="HTML"
    )
    return AWAITING_VK_GROUP


async def add_target_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    
    group_id = None
    screen_name = None
    
    club_match = re.search(r'club(\d+)', text)
    if club_match:
        group_id = int(club_match.group(1))
    
    if not group_id:
        public_match = re.search(r'public(\d+)', text)
        if public_match:
            group_id = int(public_match.group(1))
    
    if not group_id:
        screen_match = re.search(r'vk\.com/([a-zA-Z0-9_.]+)', text)
        if screen_match:
            screen_name = screen_match.group(1)
            if screen_name in ("club", "public", "admin", "support", "dev"):
                screen_name = None
    
    if not group_id and not screen_name:
        if re.match(r'^[a-zA-Z0-9_]{3,30}$', text):
            screen_name = text
    
    if not group_id and not screen_name:
        await update.message.reply_text(
            "❌ Не удалось распознать группу.\n"
            "Отправьте ссылку: <code>https://vk.com/club123456</code>\n"
            "/cancel — отмена",
            parse_mode="HTML"
        )
        return AWAITING_VK_GROUP
    
    token = context.user_data.get('temp_vk_token')
    project_id = context.user_data.get('temp_project_id')
    project_name = context.user_data.get('temp_project_name')
    
    try:
        async with aiohttp.ClientSession() as session:
            if screen_name:
                params = {
                    "access_token": token,
                    "v": Config.VK_API_VERSION,
                    "screen_name": screen_name,
                }
                async with session.get("https://api.vk.com/method/utils.resolveScreenName", params=params) as resp:
                    resolve_result = await resp.json()
                
                if "error" in resolve_result:
                    await update.message.reply_text(f"❌ Группа не найдена.\n/cancel — отмена")
                    return AWAITING_VK_GROUP
                
                resolved = resolve_result.get("response", {})
                if resolved.get("type") != "group":
                    await update.message.reply_text(f"❌ Это не группа VK.\n/cancel — отмена")
                    return AWAITING_VK_GROUP
                
                group_id = abs(resolved.get("object_id", 0))
            
            if not group_id:
                await update.message.reply_text("❌ Не удалось определить ID группы.\n/cancel — отмена")
                return AWAITING_VK_GROUP
            
            params = {
                "access_token": token,
                "v": Config.VK_API_VERSION,
                "group_id": group_id,
            }
            async with session.get("https://api.vk.com/method/groups.getById", params=params) as resp:
                group_result = await resp.json()
            
            if "error" in group_result:
                await update.message.reply_text(
                    f"❌ Нет доступа к группе.\n"
                    "Убедитесь, что вы администратор группы.\n"
                    "/cancel — отмена"
                )
                return AWAITING_VK_GROUP
            
            groups = group_result.get("response", {}).get("groups", [])
            if not groups:
                await update.message.reply_text("❌ Группа не найдена.\n/cancel — отмена")
                return AWAITING_VK_GROUP
            
            group_info = groups[0]
            group_name = group_info.get("name", "Без названия")
            
    except Exception as e:
        logger.error(f"VK group check failed: {e}")
        await update.message.reply_text("❌ Ошибка при проверке группы.\n/cancel — отмена")
        return AWAITING_VK_GROUP
    
    async with AsyncSessionLocal() as session:
        target = TargetChannel(
            project_id=project_id,
            platform="vk",
            vk_token=token,
            vk_group_id=group_id,
            vk_group_name=group_name,
            channel_title=group_name,
        )
        session.add(target)
        await session.commit()
    
    await update.message.reply_text(
        f"✅ VK-цель «{group_name}» добавлена в «{project_name}»!\n\n"
        f"Теперь добавьте источники: /add_source"
    )
    
    for key in ['temp_vk_token', 'temp_project_id', 'temp_project_name']:
        context.user_data.pop(key, None)
    
    sources_count = await get_sources_count(project_id)
    if sources_count > 0:
        await send_project_ready_message(update, project_name)
    
    return ConversationHandler.END


async def my_targets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    project = await require_project(update, context)
    if not project:
        return
    
    target = await get_project_target(project.id)
    if not target:
        await update.message.reply_text(
            f"📭 В проекте «{project.name}» нет цели.\n"
            f"Добавьте: /add_target"
        )
        return
    
    if target.platform == "vk":
        text = (
            f"🎯 <b>VK-цель «{project.name}»</b>\n\n"
            f"📝 Группа: {target.vk_group_name or '—'}\n"
            f"🆔 ID: {target.vk_group_id}\n"
            f"🔗 https://vk.com/club{target.vk_group_id}\n"
        )
        if target.last_posted:
            text += f"🕐 Последний пост: {target.last_posted.strftime('%d.%m.%Y %H:%M')}\n"
    else:
        text = (
            f"🎯 <b>Цель «{project.name}»</b>\n\n"
            f"📝 {target.channel_title}\n"
            f"🆔 {target.channel_id}\n"
        )
    
    keyboard = [[InlineKeyboardButton("❌ Удалить", callback_data=f"del_target_{target.id}")]]
    
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
        )
    else:
        await update.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
        )


async def delete_target_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    target_id = int(query.data.replace("del_target_", ""))
    
    async with AsyncSessionLocal() as session:
        await session.execute(delete(TargetChannel).where(TargetChannel.id == target_id))
        await session.commit()
    
    await query.edit_message_text("✅ Цель удалёна")