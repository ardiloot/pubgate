import logging

import pytest

from pubgate._log import setup_logging


class TestSetupLogging:
    @pytest.fixture(autouse=True)
    def _reset_logging(self):
        yield
        root = logging.getLogger()
        root.handlers.clear()
        root.setLevel(logging.WARNING)
        logging.getLogger("pubgate").setLevel(logging.NOTSET)
        logging.captureWarnings(False)

    def test_default_level_is_info(self, monkeypatch):
        monkeypatch.delenv("PUBGATE_LOG_LEVEL", raising=False)
        setup_logging()
        assert logging.getLogger("pubgate").level == logging.INFO
        assert logging.getLogger().level == logging.WARNING

    def test_debug_level_from_env(self, monkeypatch):
        monkeypatch.setenv("PUBGATE_LOG_LEVEL", "DEBUG")
        setup_logging()
        assert logging.getLogger("pubgate").level == logging.DEBUG
        assert logging.getLogger().level == logging.WARNING

    def test_invalid_env_falls_back_to_info(self, monkeypatch):
        monkeypatch.setenv("PUBGATE_LOG_LEVEL", "BOGUS")
        setup_logging()
        assert logging.getLogger("pubgate").level == logging.INFO
        assert logging.getLogger().level == logging.WARNING

    def test_idempotent_no_duplicate_handlers(self, monkeypatch):
        monkeypatch.delenv("PUBGATE_LOG_LEVEL", raising=False)
        setup_logging()
        setup_logging()
        root = logging.getLogger()
        assert len(root.handlers) == 1  # single stderr handler

    def test_info_written_to_stderr(self, monkeypatch, capsys):
        monkeypatch.delenv("PUBGATE_LOG_LEVEL", raising=False)
        setup_logging()
        logger = logging.getLogger("pubgate.test")
        logger.info("test message")
        captured = capsys.readouterr()
        assert "test message" in captured.err

    def test_debug_hidden_at_info_level(self, monkeypatch, capsys):
        monkeypatch.delenv("PUBGATE_LOG_LEVEL", raising=False)
        setup_logging()
        logger = logging.getLogger("pubgate.test")
        logger.debug("hidden message")
        captured = capsys.readouterr()
        assert "hidden message" not in captured.err

    def test_debug_written_at_debug_level(self, monkeypatch, capsys):
        monkeypatch.setenv("PUBGATE_LOG_LEVEL", "DEBUG")
        setup_logging()
        logger = logging.getLogger("pubgate.test")
        logger.debug("visible debug")
        captured = capsys.readouterr()
        assert "visible debug" in captured.err

    def test_capture_warnings_enabled(self, monkeypatch, capsys):
        monkeypatch.delenv("PUBGATE_LOG_LEVEL", raising=False)
        setup_logging()
        import warnings

        warnings.warn("test deprecation", DeprecationWarning, stacklevel=1)
        captured = capsys.readouterr()
        assert "test deprecation" in captured.err
