.PHONY: help install models build up down logs test lint clean

help:
	@echo "Fortress Captcha:"
	@echo "  install   pip install -r requirements.txt"
	@echo "  models    python scripts/download_models.py"
	@echo "  up        docker compose up -d"
	@echo "  down      docker compose down"
	@echo "  logs      docker compose logs -f"
	@echo "  test      pytest"
	@echo "  lint      ruff check ."

install:
	pip install -r requirements.txt

models:
	python scripts/download_models.py

build:
	docker compose build

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f --tail=100

test:
	pytest tests/ -v

lint:
	ruff check .

clean:
	docker compose down -v
