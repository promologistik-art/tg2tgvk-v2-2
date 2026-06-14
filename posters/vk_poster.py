"""
VK Poster — публикация постов в VK группы
Использует чистый aiohttp (без vk_api), асинхронный, как весь проект.
"""
import os
import logging
import aiohttp
from datetime import datetime
from typing import Optional
from sqlalchemy import select, update
from database import AsyncSessionLocal
from models import PostQueue, PublishedPost, TargetChannel, Project
from utils import clean_caption
from config import Config

logger = logging.getLogger(__name__)


class VKPoster:
    """Публикация постов в VK-группы."""
    
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
    
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60)
            )
        return self._session
    
    async def add_to_queue(
        self, project_id: int, target_channel_id: int,
        post_data: dict, scheduled_time: datetime, platform: str = "vk"
    ):
        """Добавить пост в очередь (совместимо с TelegramPoster)."""
        async with AsyncSessionLocal() as session:
            queue_item = PostQueue(
                project_id=project_id,
                target_channel_id=target_channel_id,
                platform=platform,
                post_data=post_data,
                scheduled_time=scheduled_time,
                status="pending"
            )
            session.add(queue_item)
            await session.commit()
            logger.info(f"📨 VK post queued for project {project_id}")

    async def publish_post(self, queue_item: PostQueue) -> bool:
        """Опубликовать пост в VK. Возвращает True при успехе."""
        target = None
        signature = None
        access_token = None
        group_id = None
        
        # === Получаем данные цели и проекта ===
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(TargetChannel).where(TargetChannel.id == queue_item.target_channel_id)
                )
                target = result.scalar_one_or_none()
                
                if not target:
                    await self._mark_failed(queue_item, "Целевой канал не найден")
                    return False
                
                if not target.vk_token:
                    await self._mark_failed(queue_item, "VK токен не указан")
                    return False
                
                if not target.vk_group_id:
                    await self._mark_failed(queue_item, "VK group_id не указан")
                    return False
                
                access_token = target.vk_token
                group_id = abs(target.vk_group_id)  # на всякий случай
                
                result = await session.execute(
                    select(Project).where(Project.id == queue_item.project_id)
                )
                project = result.scalar_one_or_none()
                signature = project.signature if project else None
        except Exception as e:
            logger.error(f"Failed to get target info: {e}")
            await self._mark_failed(queue_item, "Ошибка получения данных канала")
            return False
        
        # === Подготовка текста ===
        post_data = queue_item.post_data
        remove_text = post_data.get("remove_original_text", False)
        
        exclude_phrases_str = post_data.get("exclude_phrases", "")
        if exclude_phrases_str:
            exclude_phrases = [p.strip() for p in exclude_phrases_str.split(",") if p.strip()]
        else:
            exclude_phrases = None
        
        original_text = clean_caption(post_data.get("text", ""), exclude_phrases)
        
        if remove_text:
            caption = ""
        else:
            caption = original_text
        
        # Подпись проекта
        if signature:
            if caption:
                caption += f"\n\n{signature}"
            else:
                caption = signature
        
        # Источник
        if Config.SHOW_SOURCE_SIGNATURE:
            source = post_data.get("source_username", "")
            if source:
                source_text = f"📡 @{source}"
                if caption:
                    caption += f"\n\n{source_text}"
                else:
                    caption = source_text
        
        # === Проверка фильтра медиа ===
        media_path = post_data.get("media_path")
        media_type = post_data.get("media_type")
        media_filter = post_data.get("media_filter", "all")
        
        has_media = media_path and os.path.exists(media_path)
        has_original_text = bool(original_text.strip())
        
        if media_filter == "photo_only":
            if not has_media or media_type != "photo":
                await self._mark_failed(queue_item, f"Фильтр: только фото, но медиа тип {media_type}")
                return False
        elif media_filter == "video_only":
            if not has_media or media_type != "video":
                await self._mark_failed(queue_item, f"Фильтр: только видео, но медиа тип {media_type}")
                return False
        
        # Пустой пост без медиа
        if not has_media and not has_original_text:
            await self._mark_failed(queue_item, "Нет медиа и нет текста — только подпись")
            return False
        
        # === Публикация ===
        try:
            http = await self._get_session()
            
            if has_media:
                if media_type == "photo":
                    success = await self._upload_photo(http, access_token, group_id, media_path, caption)
                elif media_type == "video":
                    success = await self._upload_video(http, access_token, group_id, media_path, caption)
                else:
                    # Документ — пробуем как фото
                    success = await self._upload_photo(http, access_token, group_id, media_path, caption)
            else:
                success = await self._post_text(http, access_token, group_id, caption)
            
            if success:
                # Удаляем временный файл
                if media_path and os.path.exists(media_path):
                    try:
                        os.remove(media_path)
                    except:
                        pass
                await self._mark_published(queue_item)
                logger.info(f"✅ Published VK post {queue_item.id}")
                return True
            else:
                if media_path and os.path.exists(media_path):
                    try:
                        os.remove(media_path)
                    except:
                        pass
                await self._mark_failed(queue_item, "VK API вернул ошибку")
                return False
                
        except Exception as e:
            logger.error(f"VK publish error: {e}")
            if media_path and os.path.exists(media_path):
                try:
                    os.remove(media_path)
                except:
                    pass
            error_text = str(e)[:150].replace("\n", " ")
            await self._mark_failed(queue_item, f"Ошибка VK: {error_text}")
            return False

    # ============================================================
    # ЗАГРУЗКА ФОТО
    # ============================================================
    async def _upload_photo(
        self, http: aiohttp.ClientSession,
        access_token: str, group_id: int,
        file_path: str, caption: str
    ) -> bool:
        """Загрузка фото через photos.getWallUploadServer → загрузка → photos.saveWallPhoto → wall.post."""
        try:
            # 1. Получаем upload_url
            upload_url = await self._get_upload_server(http, access_token, group_id)
            if not upload_url:
                return False
            
            # 2. Загружаем фото на upload_url
            file_size = os.path.getsize(file_path)
            logger.info(f"📤 Uploading photo to VK: {file_size} bytes")
            
            with open(file_path, "rb") as f:
                form = aiohttp.FormData()
                form.add_field("photo", f, filename=os.path.basename(file_path))
                
                async with http.post(upload_url, data=form) as resp:
                    upload_result = await resp.json()
            
            if "error" in upload_result:
                logger.error(f"VK upload error: {upload_result['error']}")
                return False
            
            photo_data = upload_result.get("photo")
            server = upload_result.get("server")
            photo_hash = upload_result.get("hash")
            
            if not all([photo_data, server, photo_hash]):
                logger.error(f"VK upload: missing fields in response: {upload_result}")
                return False
            
            # 3. Сохраняем фото
            params = {
                "access_token": access_token,
                "v": Config.VK_API_VERSION,
                "group_id": group_id,
                "photo": photo_data,
                "server": server,
                "hash": photo_hash,
            }
            async with http.post("https://api.vk.com/method/photos.saveWallPhoto", params=params) as resp:
                save_result = await resp.json()
            
            if "error" in save_result:
                logger.error(f"VK saveWallPhoto error: {save_result['error']}")
                return False
            
            saved_photos = save_result.get("response", [])
            if not saved_photos:
                logger.error("VK saveWallPhoto: no photos in response")
                return False
            
            # 4. Публикуем пост с фото
            photo = saved_photos[0]
            attachment = f"photo{photo['owner_id']}_{photo['id']}"
            
            return await self._wall_post(http, access_token, group_id, caption, attachment)
            
        except Exception as e:
            logger.error(f"VK photo upload error: {e}")
            return False

    # ============================================================
    # ЗАГРУЗКА ВИДЕО
    # ============================================================
    async def _upload_video(
        self, http: aiohttp.ClientSession,
        access_token: str, group_id: int,
        file_path: str, caption: str
    ) -> bool:
        """Загрузка видео через video.save → загрузка на URL → wall.post."""
        try:
            file_size = os.path.getsize(file_path)
            file_name = os.path.basename(file_path)
            logger.info(f"📤 Uploading video to VK: {file_size} bytes")
            
            # 1. Получаем параметры для загрузки
            params = {
                "access_token": access_token,
                "v": Config.VK_API_VERSION,
                "group_id": group_id,
                "name": file_name,
            }
            async with http.get("https://api.vk.com/method/video.save", params=params) as resp:
                save_result = await resp.json()
            
            if "error" in save_result:
                logger.error(f"VK video.save error: {save_result['error']}")
                return await self._fallback_text(http, access_token, group_id, caption)
            
            upload_info = save_result.get("response", {})
            upload_url = upload_info.get("upload_url")
            
            if not upload_url:
                logger.error("VK video.save: no upload_url")
                return await self._fallback_text(http, access_token, group_id, caption)
            
            # 2. Загружаем видео на upload_url
            with open(file_path, "rb") as f:
                form = aiohttp.FormData()
                form.add_field("video_file", f, filename=file_name)
                
                async with http.post(upload_url, data=form) as resp:
                    upload_result = await resp.json()
            
            if "error" in upload_result:
                logger.error(f"VK video upload error: {upload_result['error']}")
                return await self._fallback_text(http, access_token, group_id, caption)
            
            video_id = upload_result.get("video_id")
            owner_id = upload_info.get("owner_id", -group_id)
            
            if not video_id:
                logger.error("VK video upload: no video_id in response")
                return await self._fallback_text(http, access_token, group_id, caption)
            
            # 3. Публикуем пост с видео
            attachment = f"video{owner_id}_{video_id}"
            return await self._wall_post(http, access_token, group_id, caption, attachment)
            
        except Exception as e:
            logger.error(f"VK video upload error: {e}")
            return await self._fallback_text(http, access_token, group_id, caption)

    # ============================================================
    # ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ
    # ============================================================
    async def _get_upload_server(
        self, http: aiohttp.ClientSession, access_token: str, group_id: int
    ) -> Optional[str]:
        """Получить upload_url для фото."""
        params = {
            "access_token": access_token,
            "v": Config.VK_API_VERSION,
            "group_id": group_id,
        }
        async with http.get("https://api.vk.com/method/photos.getWallUploadServer", params=params) as resp:
            result = await resp.json()
        
        if "error" in result:
            logger.error(f"VK getWallUploadServer error: {result['error']}")
            return None
        
        return result.get("response", {}).get("upload_url")

    async def _wall_post(
        self, http: aiohttp.ClientSession,
        access_token: str, group_id: int,
        message: str, attachment: str = None
    ) -> bool:
        """Опубликовать пост на стене."""
        params = {
            "access_token": access_token,
            "v": Config.VK_API_VERSION,
            "owner_id": -group_id,
            "from_group": 1,
            "message": message or "",
        }
        if attachment:
            params["attachment"] = attachment
        
        async with http.post("https://api.vk.com/method/wall.post", params=params) as resp:
            result = await resp.json()
        
        if "error" in result:
            logger.error(f"VK wall.post error: {result['error']}")
            # Если с фото/видео ошибка — пробуем только текст
            if attachment and message:
                logger.info("VK wall.post with attachment failed, trying text only")
                return await self._post_text(http, access_token, group_id, message)
            return False
        
        post_id = result.get("response", {}).get("post_id")
        logger.info(f"✅ VK wall.post success: post_id={post_id}")
        return True

    async def _post_text(
        self, http: aiohttp.ClientSession,
        access_token: str, group_id: int,
        message: str
    ) -> bool:
        """Опубликовать только текст."""
        params = {
            "access_token": access_token,
            "v": Config.VK_API_VERSION,
            "owner_id": -group_id,
            "from_group": 1,
            "message": message or "",
        }
        async with http.post("https://api.vk.com/method/wall.post", params=params) as resp:
            result = await resp.json()
        
        if "error" in result:
            logger.error(f"VK wall.post (text) error: {result['error']}")
            return False
        
        post_id = result.get("response", {}).get("post_id")
        logger.info(f"✅ VK wall.post (text) success: post_id={post_id}")
        return True

    async def _fallback_text(
        self, http: aiohttp.ClientSession,
        access_token: str, group_id: int,
        message: str
    ) -> bool:
        """Fallback: только текст при ошибке загрузки медиа."""
        if message:
            logger.info("Falling back to text-only post")
            return await self._post_text(http, access_token, group_id, message)
        return False

    # ============================================================
    # СЛУЖЕБНЫЕ МЕТОДЫ
    # ============================================================
    async def _mark_published(self, queue_item: PostQueue):
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(PostQueue)
                .where(PostQueue.id == queue_item.id)
                .values(status="published", published_at=datetime.utcnow())
            )
            published = PublishedPost(
                project_id=queue_item.project_id,
                target_channel_id=queue_item.target_channel_id,
                platform=queue_item.platform,
                source_channel_username=queue_item.post_data.get("source_username", ""),
                post_url=queue_item.post_data.get("url", ""),
                post_data=queue_item.post_data
            )
            session.add(published)
            await session.execute(
                update(TargetChannel)
                .where(TargetChannel.id == queue_item.target_channel_id)
                .values(last_posted=datetime.utcnow())
            )
            await session.commit()

    async def _mark_failed(self, queue_item: PostQueue, error_message: str):
        clean_error = error_message[:150].replace("\n", " ").strip()
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(PostQueue)
                .where(PostQueue.id == queue_item.id)
                .values(status="failed", error_message=clean_error)
            )
            await session.commit()
            logger.warning(f"❌ VK post {queue_item.id} failed: {clean_error}")

    async def stop(self):
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("🔴 VKPoster stopped")