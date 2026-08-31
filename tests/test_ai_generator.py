"""Тесты для генератора ироничных алкогольных текстов через Mistral API."""
import pytest

from app.ai_generator import AIError, AIGenerator, generate_alcohol_post


class _FakeResponse:
    """Минимальный фейковый ответ requests.Response для тестов."""

    def __init__(self, status, json_body):
        self.status_code = status
        self._json = json_body
        self.text = str(json_body)

    def json(self):
        return self._json


@pytest.fixture
def generator():
    return AIGenerator(api_key="test-key", model="mistral-small-latest")


class TestGenerateAlcoholPost:
    def test_returns_string(self, requests_mock, generator):
        """Должен вернуть строку с юмористическим текстом."""
        requests_mock.post(
            "https://api.mistral.ai/v1/chat/completions",
            json={
                "choices": [
                    {
                        "message": {
                            "content": "Сегодня день гранёного стакана — "
                            "священного сосуда, который вмещает и водку, "
                            "и надежды на завтрашнее утро!"
                        }
                    }
                ]
            },
        )
        result = generate_alcohol_post(
            holiday_title="День гранёного стакана",
            holiday_description="Праздник гранёного стакана, изобретённого в СССР.",
            api_key="test-key",
        )
        assert isinstance(result, str)
        assert len(result) > 0

    def test_prompt_contains_holiday_info(self, requests_mock, generator, mocker):
        """Промпт должен содержать название и описание праздника."""
        import json

        import requests

        from app import ai_generator

        sent_payload = {}
        original_post = requests.post

        def fake_post(url, *args, **kwargs):
            sent_payload["url"] = url
            sent_payload["json"] = kwargs.get("json", {})
            return original_post(url, *args, **kwargs)

        # Перехватываем requests.post, чтобы увидеть отправленный payload
        mocker.patch.object(ai_generator.requests, "post", side_effect=fake_post)

        requests_mock.post(
            "https://api.mistral.ai/v1/chat/completions",
            json={"choices": [{"message": {"content": "Хороший пост"}}]},
        )
        generate_alcohol_post(
            holiday_title="День гранёного стакана",
            holiday_description="Праздник про стакан",
            api_key="test-key",
        )
        body = json.dumps(sent_payload["json"], ensure_ascii=False)
        assert "День гранёного стакана" in body
        assert "Праздник про стакан" in body

    def test_api_error_raises_ai_error(self, requests_mock, generator):
        """Ошибка API должна поднимать AIError."""
        requests_mock.post(
            "https://api.mistral.ai/v1/chat/completions", status=401, body='{"error":"Invalid API Key"}'
        )
        with pytest.raises(AIError):
            generate_alcohol_post(
                holiday_title="Тест", holiday_description="Тест",
                api_key="bad-key",
            )

    def test_missing_api_key_raises_error(self):
        """Без ключа должен подниматься AIError."""
        with pytest.raises(AIError):
            generate_alcohol_post(
                holiday_title="Тест", holiday_description="Тест", api_key=""
            )


class TestAIGenerator:
    def test_init_with_api_key(self):
        g = AIGenerator(api_key="sk-test")
        assert g.api_key == "sk-test"

    def test_default_model(self):
        g = AIGenerator(api_key="sk-test")
        assert g.model == "mistral-small-latest"

    def test_generate_returns_content(self, requests_mock):
        g = AIGenerator(api_key="sk-test")
        requests_mock.post(
            "https://api.mistral.ai/v1/chat/completions",
            json={"choices": [{"message": {"content": "Ироничный текст"}}]},
        )
        result = g.generate("Расскажи про праздник")
        assert result == "Ироничный текст"

    def test_generate_retries_on_429(self, requests_mock, mocker):
        """При 429 должен быть повторный запрос."""
        from app import ai_generator

        g = AIGenerator(api_key="sk-test", max_retries=2)
        # Первый ответ 429, второй — 200
        mocker.patch.object(
            ai_generator.requests,
            "post",
            side_effect=(
                _FakeResponse(429, {}),
                _FakeResponse(200, {"choices": [{"message": {"content": "Успех"}}]}),
            ),
        )
        result = g.generate("тест")
        assert result == "Успех"
        assert ai_generator.requests.post.call_count == 2


class TestGenerateForStatus:
    def _mock_raw(self, mocker, generator, raw):
        return mocker.patch.object(generator, "generate", return_value=raw)

    def test_parses_plain_json(self, mocker, generator):
        raw = '{"label": "МИФ", "body": "текст", "claims_used": ["c1"], "unsupported_claims": [], "visual_brief": "без текста"}'
        self._mock_raw(mocker, generator, raw)
        result = generator.generate_for_status("unverified", "Т", "О", [{"claim_id": "c1", "text": "факт"}])
        assert result.label == "МИФ" and result.body == "текст"

    def test_parses_fenced_json(self, mocker, generator):
        """Модель может обернуть JSON в ```json ... ```."""
        raw = 'Вот результат:\n```json\n{"label": "МИФ", "body": "текст", "claims_used": ["c1"], "unsupported_claims": [], "visual_brief": "без текста"}\n```'
        self._mock_raw(mocker, generator, raw)
        result = generator.generate_for_status("unverified", "Т", "О", [{"claim_id": "c1", "text": "факт"}])
        assert result.label == "МИФ" and result.body == "текст"

    def test_rejects_wrong_label(self, mocker, generator):
        raw = '{"label": "РАЗБОР", "body": "текст", "claims_used": [], "unsupported_claims": [], "visual_brief": "без текста"}'
        self._mock_raw(mocker, generator, raw)
        with pytest.raises(AIError):
            generator.generate_for_status("unverified", "Т", "О")

    def test_status_generation_requests_json_response_format(self, mocker, generator):
        """Запрос к Mistral для статусной генерации должен включать response_format json_object."""
        from app import ai_generator

        mocker.patch.object(
            ai_generator.requests,
            "post",
            return_value=_FakeResponse(200, {"choices": [{"message": {"content": '{"label": "МИФ", "body": "т", "claims_used": [], "unsupported_claims": [], "visual_brief": ""}'}}]}),
        )
        generator.generate_for_status("unverified", "Т", "О")
        payload = ai_generator.requests.post.call_args.kwargs["json"]
        assert payload.get("response_format") == {"type": "json_object"}

    def test_retries_with_correction_when_claims_used_holds_text(self, mocker, generator):
        """Если модель положила в claims_used тексты вместо идентификаторов — одна повторная попытка с уточнением."""
        bad = '{"label": "МИФ", "body": "текст", "claims_used": ["В 1914 году был сухой закон"], "unsupported_claims": [], "visual_brief": "без текста"}'
        good = '{"label": "МИФ", "body": "текст", "claims_used": ["c1"], "unsupported_claims": [], "visual_brief": "без текста"}'
        gen_mock = mocker.patch.object(generator, "generate", side_effect=[bad, good])

        result = generator.generate_for_status("unverified", "Т", "О", [{"claim_id": "c1", "text": "факт"}])

        assert result.label == "МИФ"
        assert gen_mock.call_count == 2
        assert "Исправь формат" in gen_mock.call_args.args[0]
