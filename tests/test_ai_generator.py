"""Тесты для генератора ироничных алкогольных текстов через Gemini API."""
import json

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
    return AIGenerator(api_key="test-key", model="gemini-3.5-flash-lite")


class TestGenerateAlcoholPost:
    def test_returns_string(self, requests_mock, generator):
        """Должен вернуть строку с юмористическим текстом."""
        requests_mock.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent",
            json={
                "candidates": [{"content": {"parts": [{"text": "Сегодня день гранёного стакана — "
                            "священного сосуда, который вмещает и водку, "
                            "и надежды на завтрашнее утро!"}]}}]
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
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent",
            json={"candidates": [{"content": {"parts": [{"text": "Хороший пост"}]}}]},
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
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent", status=401, body='{"error":"Invalid API Key"}'
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
        assert g.model == "gemini-3.5-flash-lite"

    def test_generate_returns_content(self, requests_mock):
        g = AIGenerator(api_key="sk-test")
        requests_mock.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent",
            json={"candidates": [{"content": {"parts": [{"text": "Ироничный текст"}]}}]},
        )
        result = g.generate("Расскажи про праздник")
        assert result == "Ироничный текст"

    def test_generate_uses_gemini_header_and_content_payload(self, requests_mock):
        g = AIGenerator(api_key="test-key", model="models/gemini-3.5-flash-lite")
        requests_mock.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent",
            json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]},
        )

        assert g.generate("prompt") == "ok"
        request = requests_mock.calls[0].request
        assert request.headers["x-goog-api-key"] == "test-key"
        assert json.loads(request.body)["systemInstruction"]["parts"]
        assert json.loads(request.body)["contents"][0]["parts"][0]["text"] == "prompt"

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
                _FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": "Успех"}]}}]}),
            ),
        )
        result = g.generate("тест")
        assert result == "Успех"
        assert ai_generator.requests.post.call_count == 2


class TestGenerateRecipe:
    def _joke_payload(self, *, substituted_amount=45):
        return json.dumps({
            "recipe_type": "joke",
            "title": "Мартини с характером",
            "caption": "Коктейль для переговоров с понедельником.",
            "original_ingredients": [
                {"name": "джин", "amount": 45, "unit": "мл"},
                {"name": "сухой вермут", "amount": 15, "unit": "мл"},
            ],
            "ingredients": [
                {"name": "томатный сок", "amount": substituted_amount, "unit": "мл"},
                {"name": "сухой вермут", "amount": 15, "unit": "мл"},
            ],
            "substitutions": [{"from": "джин", "to": "томатный сок"}],
            "instructions": ["Охладить стакан и аккуратно перемешать."]
        }, ensure_ascii=False)

    def test_joke_recipe_preserves_measures_and_renders_structured_ingredients(self, mocker):
        generator = AIGenerator(api_key="test-key")
        mocker.patch.object(generator, "generate", return_value=self._joke_payload())

        result = generator.generate_recipe("joke")

        assert "Иронично" not in result
        assert "45 мл томатный сок" in result
        assert "15 мл сухой вермут" in result

    def test_joke_recipe_retries_then_rejects_changed_proportion(self, mocker):
        generator = AIGenerator(api_key="test-key")
        call = mocker.patch.object(generator, "generate", side_effect=[
            self._joke_payload(substituted_amount=60),
            self._joke_payload(substituted_amount=60),
        ])

        with pytest.raises(AIError, match="amounts and units"):
            generator.generate_recipe("joke")

        assert call.call_count == 2
        assert "шуточные" in call.call_args.kwargs["system_instruction"]

    def test_classic_recipe_does_not_allow_substitutions(self, mocker):
        generator = AIGenerator(api_key="test-key")
        payload = json.dumps({
            "recipe_type": "classic",
            "title": "Дайкири",
            "caption": "Классический вариант.",
            "original_ingredients": [{"name": "ром", "amount": 60, "unit": "мл"}],
            "ingredients": [{"name": "ром", "amount": 60, "unit": "мл"}],
            "substitutions": [],
            "instructions": ["Взбить со льдом и процедить."]
        }, ensure_ascii=False)
        call = mocker.patch.object(generator, "generate", return_value=payload)

        result = generator.generate_recipe("classic")

        assert "60 мл ром" in result
        assert "стандартные пропорции" in call.call_args.args[0]
        assert "нейтрально" in call.call_args.kwargs["system_instruction"]

    def test_classic_recipe_rejects_any_substitution(self, mocker):
        generator = AIGenerator(api_key="test-key")
        payload = json.dumps({
            "recipe_type": "classic",
            "title": "Дайкири",
            "caption": "Классический вариант.",
            "original_ingredients": [{"name": "ром", "amount": 60, "unit": "мл"}],
            "ingredients": [{"name": "джин", "amount": 60, "unit": "мл"}],
            "substitutions": [{"from": "ром", "to": "джин"}],
            "instructions": ["Взбить со льдом и процедить."]
        }, ensure_ascii=False)
        mocker.patch.object(generator, "generate", side_effect=[payload, payload])

        with pytest.raises(AIError, match="classic recipe must not change ingredients"):
            generator.generate_recipe("classic")

    def test_invalid_recipe_type_is_rejected(self):
        with pytest.raises(AIError, match="unsupported recipe type"):
            AIGenerator(api_key="test-key").generate_recipe("other")


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
        """Запрос к Gemini для статусной генерации должен включать response_format json_object."""
        from app import ai_generator

        mocker.patch.object(
            ai_generator.requests,
            "post",
            return_value=_FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": '{"label": "МИФ", "body": "т", "claims_used": [], "unsupported_claims": [], "visual_brief": ""}'}]}}]}),
        )
        generator.generate_for_status("unverified", "Т", "О")
        payload = ai_generator.requests.post.call_args.kwargs["json"]
        assert payload["generationConfig"]["responseMimeType"] == "application/json"
        assert "systemInstruction" in payload

    def test_retries_with_correction_on_wrong_label(self, mocker, generator):
        """Неверная маркировка — одна повторная попытка с уточнением, затем успех."""
        bad = '{"label": "РАЗБОР", "body": "текст", "claims_used": [], "unsupported_claims": [], "visual_brief": ""}'
        good = '{"label": "МИФ", "body": "текст", "claims_used": [], "unsupported_claims": [], "visual_brief": ""}'
        gen_mock = mocker.patch.object(generator, "generate", side_effect=[bad, good])

        result = generator.generate_for_status("unverified", "Т", "О")

        assert result.label == "МИФ"
        assert gen_mock.call_count == 2
        assert "Исправь формат" in gen_mock.call_args.args[0]

    def test_unsupported_claims_do_not_block_publication(self, mocker, generator):
        """Метаданные claims в пост не попадают: unsupported/неизвестные id — не блокировка."""
        raw = '{"label": "МИФ", "body": "текст", "claims_used": ["c1"], "unsupported_claims": ["c2"], "visual_brief": "без текста"}'
        self._mock_raw(mocker, generator, raw)

        result = generator.generate_for_status(
            "unverified", "Т", "О",
            [{"claim_id": "c1", "text": "ф"}, {"claim_id": "c2", "text": "ф2"}],
        )

        assert result.label == "МИФ"
        assert result.unsupported_claims == ("c2",)
