# Команды проекта. На Windows удобнее вызывать напрямую через .\.venv\Scripts\python.exe
# (см. README), Makefile оставлен для CI и для Unix-разработчиков.

PY ?= .venv/Scripts/python.exe
SRC := src/hirebot

.DEFAULT_GOAL := help
.PHONY: help setup install check lint fmt types test test-unit test-integration cov \
        up down migrate db-init demo serve worker doctor clean

help: ## Показать список команд
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

setup: ## Создать venv и установить зависимости
	uv venv --python 3.12 .venv
	uv pip install -e ".[dev]"

install: ## Установить/обновить зависимости в существующий venv
	uv pip install -e ".[dev]"

check: lint types test ## Полная проверка (линт + типы + тесты)

lint: ## Линтер
	$(PY) -m ruff check src tests
	$(PY) -m ruff format --check src tests

fmt: ## Автоформатирование и автофиксы
	$(PY) -m ruff check --fix src tests
	$(PY) -m ruff format src tests

types: ## Проверка типов
	$(PY) -m mypy $(SRC)

test: ## Все тесты, кроме сетевых
	$(PY) -m pytest -q -m "not network"

test-unit: ## Только unit-тесты
	$(PY) -m pytest tests/unit -q

test-integration: ## Интеграционные тесты (нужна БД)
	$(PY) -m pytest tests/integration -q -m integration

cov: ## Тесты с покрытием
	$(PY) -m pytest --cov=$(SRC) --cov-report=term-missing --cov-report=xml -m "not network"

up: ## Поднять Postgres и Redis
	docker compose up -d postgres redis

down: ## Остановить инфраструктуру
	docker compose down

migrate: ## Применить миграции
	$(PY) -m alembic upgrade head

db-init: ## Создать схему без Alembic (SQLite-режим)
	$(PY) -m hirebot.cli db init

demo: ## Сквозной демо-прогон на fake-адаптерах (без ключей и сети)
	$(PY) -m hirebot.cli demo

serve: ## Запустить API
	$(PY) -m hirebot.cli serve

worker: ## Запустить воркер
	$(PY) -m hirebot.cli worker

doctor: ## Проверить конфигурацию окружения
	$(PY) -m hirebot.cli doctor

clean: ## Удалить кэши
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
