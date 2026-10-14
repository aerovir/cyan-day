"""Публикация постов в VK-группу через vk_api.

Умеет постить текст и картинки (загружает их через VkUpload.photo_wall).
"""

from __future__ import annotations

import io
import logging
import os
from collections.abc import Mapping
from urllib.parse import quote, quote_plus
from typing import Any, Optional

import vk_api
from vk_api.exceptions import VkApiError

logger = logging.getLogger(__name__)
POST_ID_ERROR = "VK returned an invalid post result"


class VKUnknownError(Exception):
    """VK may have accepted the post, but its result is ambiguous."""


class VKResultError(VKUnknownError):
    """VK returned a malformed response after the request was sent."""


class VKTransportError(VKUnknownError):
    """The transport failed after the VK request may have been accepted."""


class VKError(Exception):
    """Ошибка публикации в VK."""


class VKPhotoUploadError(VKError):
    """Photo upload failed before wall.post was called."""

    def __init__(self, message: str, code: int | None = None, reason: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason or message


class VKPublisher:
    """Публикация постов в сообщество VK."""

    def __init__(self, token: str, group_id: int) -> None:
        """Инициализировать публикатор.

        Args:
            token: Токен сообщества (с правами wall, photos, groups).
            group_id: Положительный ID группы.

        Raises:
            VKError: Если токен пустой или не удалось авторизоваться.
        """
        if not token:
            raise VKError("VK_TOKEN не задан")
        self._token = token
        self.group_id = int(group_id)
        self.owner_id = -self.group_id  # для групп owner_id отрицательный

        try:
            self._session = vk_api.VkApi(token=token)
            self.api = self._session.get_api()
        except VkApiError as exc:
            raise VKError(f"Ошибка авторизации VK: {exc}") from exc

    def post(
        self,
        message: str,
        photo_path: Optional[str] = None,
    ) -> int:
        """Опубликовать пост на стену группы.

        Args:
            message: Текст поста.
            photo_path: Путь к файлу-картинке (или None).

        Returns:
            ID созданного поста.

        Raises:
            VKError: При ошибке публикации.
        """
        attachment = None
        if photo_path:
            attachment = self._upload_photo(photo_path)

        try:
            params = {
                "owner_id": self.owner_id,
                "message": message,
            }
            if attachment:
                params["attachment"] = attachment
            response = self.api.wall.post(**params)
        except VkApiError as exc:
            raise VKError(f"Ошибка публикации в VK: {exc}") from exc
        except (TimeoutError, ConnectionError, OSError) as exc:
            raise VKTransportError(str(exc)) from exc

        if not isinstance(response, Mapping):
            raise VKResultError(POST_ID_ERROR)
        post_id: Any = response.get("post_id")
        if isinstance(post_id, bool) or not isinstance(post_id, int) or post_id <= 0:
            raise VKResultError(POST_ID_ERROR)
        logger.info("Опубликован пост #%s (attachment: %s)", post_id, attachment)
        return post_id

    def _safe_error_text(self, error: object) -> str:
        text = str(error)
        for secret in (self._token, quote(self._token, safe=""), quote_plus(self._token)):
            if secret:
                text = text.replace(secret, "[REDACTED]")
        return text[:500]

    def _upload_photo(self, photo_path: str) -> str:
        """Загрузить фото на сервер VK для поста на стену.

        Returns:
            Строка attachment вида ``photo{owner_id}_{id}``.
        """
        if not os.path.exists(photo_path):
            raise VKPhotoUploadError(f"Файл не найден: {photo_path}")

        upload = vk_api.VkUpload(self._session)
        try:
            with open(photo_path, "rb") as f:
                photo_data = io.BytesIO(f.read())
            photo = upload.photo_wall(
                photos=photo_data,
                group_id=self.group_id,
            )
        except VkApiError as exc:
            code = getattr(exc, "code", None)
            reason = self._safe_error_text(exc)
            raise VKPhotoUploadError("Ошибка загрузки фото в VK", code=code, reason=reason) from exc
        except (OSError, TypeError, IndexError, KeyError) as exc:
            raise VKPhotoUploadError(f"Ошибка подготовки фото для VK: {exc}") from exc

        try:
            item = photo[0]
            owner_id = item["owner_id"]
            photo_id = item["id"]
        except (IndexError, KeyError, TypeError) as exc:
            raise VKPhotoUploadError("VK returned an invalid photo upload result") from exc
        return f"photo{owner_id}_{photo_id}"
