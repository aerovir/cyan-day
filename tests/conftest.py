"""Общие фикстуры pytest."""
import os
import sys

import pytest
import responses

# Чтобы импорты `from app...` работали из корня проекта
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def requests_mock():
    """Mock для HTTP-запросов через библиотеку responses."""
    with responses.RequestsMock() as rsps:
        yield rsps
