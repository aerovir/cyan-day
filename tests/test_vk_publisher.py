"""Тесты для публикации постов в VK через vk_api."""
import pytest

from app.vk_publisher import VKError, VKPublisher


@pytest.fixture
def vk_session_mock(mocker):
    """Мок VkApi-сессии."""
    session = mocker.Mock()
    return session


@pytest.fixture
def publisher(vk_session_mock, mocker):
    """VKPublisher с замоканной сессией."""
    mocker.patch("app.vk_publisher.vk_api.VkApi", return_value=vk_session_mock)
    return VKPublisher(token="test-token", group_id=123456789)


class TestVKPublisher:
    def test_init_creates_session(self, vk_session_mock, mocker):
        """VKPublisher должен создавать VkApi с токеном."""
        mocker.patch("app.vk_publisher.vk_api.VkApi", return_value=vk_session_mock)
        VKPublisher(token="test-token", group_id=123456789)
        from app import vk_publisher
        vk_publisher.vk_api.VkApi.assert_called_once_with(token="test-token")

    def test_owner_id_is_negative(self, publisher):
        """owner_id для группы должен быть отрицательным."""
        assert publisher.owner_id == -123456789

    def test_api_available(self, vk_session_mock, publisher):
        """Метод get_api должен возвращать API."""
        api = vk_session_mock.get_api.return_value
        assert publisher.api is api

    def test_post_without_photo(self, publisher, vk_session_mock):
        """Пост без фото: wall.post вызывается с owner_id и message."""
        api = vk_session_mock.get_api.return_value
        api.wall.post.return_value = {"post_id": 123}

        post_id = publisher.post(
            message="Сегодня день гранёного стакана",
        )

        assert post_id == 123
        api.wall.post.assert_called_once()
        call_kwargs = api.wall.post.call_args.kwargs
        assert call_kwargs["owner_id"] == -123456789
        assert "Сегодня день гранёного стакана" in call_kwargs["message"]

    def test_post_with_photo(self, publisher, vk_session_mock, mocker, tmp_path):
        """Пост с фото: загружается через photo_wall и добавляется attachment."""
        api = vk_session_mock.get_api.return_value
        api.wall.post.return_value = {"post_id": 456}

        upload = mocker.patch("app.vk_publisher.vk_api.VkUpload").return_value
        upload.photo_wall.return_value = [{"owner_id": -123456789, "id": 999}]

        # Создаём реальный файл-картинку
        photo = tmp_path / "photo.jpg"
        photo.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")

        post_id = publisher.post(
            message="Пост с картинкой",
            photo_path=str(photo),
        )

        assert post_id == 456
        upload.photo_wall.assert_called_once()
        api.wall.post.assert_called_once()
        call_kwargs = api.wall.post.call_args.kwargs
        assert "photo-123456789_999" in call_kwargs["attachment"]

    def test_post_without_photo_has_no_attachment(self, publisher, vk_session_mock):
        """Пост без фото не должен иметь attachment."""
        api = vk_session_mock.get_api.return_value
        api.wall.post.return_value = {"post_id": 1}

        publisher.post(message="Текст без фото")

        call_kwargs = api.wall.post.call_args.kwargs
        assert call_kwargs.get("attachment") is None

    def test_vk_error_raises(self, publisher, vk_session_mock):
        """Ошибка VK должна подниматься как VKError."""
        api = vk_session_mock.get_api.return_value
        api.wall.post.side_effect = vk_api_error("Не хватает прав")

        with pytest.raises(VKError):
            publisher.post(message="Тест")

    def test_missing_token_raises(self, vk_session_mock, mocker):
        """Без токена VKPublisher должен падать."""
        mocker.patch("app.vk_publisher.vk_api.VkApi", return_value=vk_session_mock)
        with pytest.raises(VKError):
            VKPublisher(token="", group_id=123456789)


def vk_api_error(msg):
    """Создать исключение в стиле vk_api.VkApiError."""
    from vk_api.exceptions import VkApiError
    return VkApiError(msg)
