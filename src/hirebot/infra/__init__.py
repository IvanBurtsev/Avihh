"""Инфраструктурный слой: БД, Redis, HTTP, очередь, время, шифрование ПДн.

Модули пакета осознанно **не** импортируются здесь: ``import hirebot.infra``
не должен тянуть Redis/SQLAlchemy/httpx. Конкретные фабрики подключаются
точечно (``hirebot.infra.cache``, ``hirebot.infra.http``, ...), а сборка
происходит в ``hirebot.container``.
"""
