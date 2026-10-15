import contextlib
import io
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from publikot.__main__ import run
from publikot.settings import ConfigError, Settings


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_busy_oauth_port_reports_cause_without_false_running_message(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0))
            occupied.listen(1)
            port = occupied.getsockname()[1]
            settings = Settings('private-test-key', 123, 42, Path(directory), oauth_client_id='123',
                                oauth_public_url='https://bot.example.com', oauth_port=port)
            client = SimpleNamespace(enable_callback_events=AsyncMock(),
                long_poll_server=AsyncMock(return_value={'ts': '1'}), send=AsyncMock())
            output = io.StringIO()
            with patch('publikot.__main__.Settings.load', return_value=settings), \
                 patch('publikot.__main__.VkMessages', return_value=client), contextlib.redirect_stdout(output):
                with self.assertRaises(ConfigError) as error:
                    await run(SimpleNamespace(check=False, identify=False))
            self.assertIn(str(port), str(error.exception))
            self.assertIn('занят', str(error.exception))
            self.assertNotIn('private-test-key', str(error.exception))
            self.assertNotIn('Публикот запущен', output.getvalue())

    async def test_check_does_not_change_long_poll_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings('private-test-key', 123, 42, Path(directory))
            client = SimpleNamespace(enable_callback_events=AsyncMock(),
                long_poll_server=AsyncMock(return_value={'ts': '1'}))
            with patch('publikot.__main__.Settings.load', return_value=settings), \
                 patch('publikot.__main__.VkMessages', return_value=client), contextlib.redirect_stdout(io.StringIO()):
                await run(SimpleNamespace(check=True, identify=False))
            client.enable_callback_events.assert_not_awaited()
            client.long_poll_server.assert_awaited_once()
