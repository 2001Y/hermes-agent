"""Tests for hermes_stay_awake — OS sleep prevention module."""

import platform
import subprocess
from unittest.mock import Mock, patch, MagicMock

import pytest

from hermes_stay_awake import StayAwake


class TestStayAwakeDisabled:
    """When disabled, StayAwake is a no-op."""

    def test_enter_does_nothing(self):
        sa = StayAwake(enabled=False)
        sa.__enter__()
        assert sa._process is None
        assert sa._original_state is None

    def test_exit_does_nothing(self):
        sa = StayAwake(enabled=False)
        sa.__enter__()
        sa.__exit__(None, None, None)
        assert sa._process is None

    def test_with_statement_is_noop(self):
        """The with statement should work transparently when disabled."""
        result = []
        with StayAwake(enabled=False):
            result.append("working")
        assert result == ["working"]


class TestStayAwakeMacOS:
    """macOS: uses caffeinate -i subprocess."""

    @patch("platform.system", return_value="Darwin")
    @patch("subprocess.Popen")
    def test_starts_caffeinate_with_correct_flags(self, mock_popen, _mock_system):
        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc

        sa = StayAwake(enabled=True)
        sa.__enter__()

        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        assert args == ["caffeinate", "-i"]
        assert sa._process is mock_proc

    @patch("platform.system", return_value="Darwin")
    @patch("subprocess.Popen")
    def test_exit_terminates_process(self, mock_popen, _mock_system):
        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc

        sa = StayAwake(enabled=True)
        sa.__enter__()
        sa.__exit__(None, None, None)

        mock_proc.terminate.assert_called_once()
        mock_proc.wait.assert_called_once_with(timeout=2)

    @patch("platform.system", return_value="Darwin")
    @patch("subprocess.Popen")
    def test_exit_kills_on_timeout(self, mock_popen, _mock_system):
        mock_proc = MagicMock()
        mock_proc.wait.side_effect = subprocess.TimeoutExpired(cmd="caffeinate", timeout=2)
        mock_popen.return_value = mock_proc

        sa = StayAwake(enabled=True)
        sa.__enter__()
        sa.__exit__(None, None, None)

        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_called_once()

    @patch("platform.system", return_value="Darwin")
    @patch("subprocess.Popen", side_effect=FileNotFoundError)
    def test_handles_caffeinate_not_found(self, mock_popen, _mock_system):
        """Should degrade gracefully when caffeinate isn't available."""
        sa = StayAwake(enabled=True)
        sa.__enter__()  # Should not raise
        assert sa._process is None
        sa.__exit__(None, None, None)  # Should be fine


class TestStayAwakeLinux:
    """Linux: uses systemd-inhibit subprocess."""

    @patch("platform.system", return_value="Linux")
    @patch("subprocess.Popen")
    def test_starts_systemd_inhibit_with_correct_args(self, mock_popen, _mock_system):
        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc

        sa = StayAwake(enabled=True)
        sa.__enter__()

        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        assert args[0] == "systemd-inhibit"
        assert "--what=sleep:idle" in args
        assert "--why=Hermes agent working" in args
        assert "--who=hermes" in args
        assert "sleep" in args
        assert "infinity" in args

    @patch("platform.system", return_value="Linux")
    @patch("subprocess.Popen", side_effect=FileNotFoundError)
    def test_handles_systemd_not_available(self, mock_popen, _mock_system):
        """Containers/WSL1/non-systemd distros degrade gracefully."""
        sa = StayAwake(enabled=True)
        sa.__enter__()
        assert sa._process is None
        sa.__exit__(None, None, None)  # No crash


class TestStayAwakeWindows:
    """Windows: uses SetThreadExecutionState."""

    @patch("platform.system", return_value="Windows")
    def test_starts_windows_inhibitor(self, _mock_system):
        mock_kernel32 = MagicMock()
        mock_kernel32.SetThreadExecutionState.return_value = 0x80000001

        with patch.dict("sys.modules", {"ctypes": MagicMock()}):
            import ctypes
            ctypes.windll.kernel32 = mock_kernel32

            sa = StayAwake(enabled=True)
            sa.__enter__()

            mock_kernel32.SetThreadExecutionState.assert_called_once()
            # ES_CONTINUOUS | ES_SYSTEM_REQUIRED = 0x80000000 | 0x00000001
            call_arg = mock_kernel32.SetThreadExecutionState.call_args[0][0]
            assert call_arg & 0x80000000  # ES_CONTINUOUS
            assert call_arg & 0x00000001  # ES_SYSTEM_REQUIRED
            assert sa._original_state == 0x80000001

    @patch("platform.system", return_value="Windows")
    def test_stop_windows_restores_state(self, _mock_system):
        mock_kernel32 = MagicMock()
        mock_kernel32.SetThreadExecutionState.return_value = 0x80000001

        with patch.dict("sys.modules", {"ctypes": MagicMock()}):
            import ctypes
            ctypes.windll.kernel32 = mock_kernel32

            sa = StayAwake(enabled=True)
            sa.__enter__()
            sa.__exit__(None, None, None)

            assert mock_kernel32.SetThreadExecutionState.call_count == 2
            # Second call should restore (ES_CONTINUOUS only)
            second_call = mock_kernel32.SetThreadExecutionState.call_args_list[1]
            assert second_call[0][0] == 0x80000000  # ES_CONTINUOUS

    @patch("platform.system", return_value="Windows")
    def test_handles_import_error(self, _mock_system):
        """If ctypes is unavailable (unlikely but defensive)."""
        # Actually ctypes is in stdlib, but let's test the exception path
        with patch("builtins.__import__", side_effect=ImportError):
            sa = StayAwake(enabled=True)
            sa.__enter__()  # Should not raise
            assert sa._original_state is None


class TestStayAwakeContextManager:
    """Test the with statement integration."""

    @patch("platform.system", return_value="Darwin")
    @patch("subprocess.Popen")
    def test_exit_called_even_on_exception(self, mock_popen, _mock_system):
        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc

        try:
            with StayAwake(enabled=True):
                raise ValueError("something broke")
        except ValueError:
            pass

        mock_proc.terminate.assert_called_once()
        mock_proc.wait.assert_called_once_with(timeout=2)


class TestStayAwakeUnknownOS:
    """Graceful degradation on unknown operating systems."""

    @patch("platform.system", return_value="FreeBSD")
    def test_unknown_os_is_noop(self, _mock_system):
        sa = StayAwake(enabled=True)
        sa.__enter__()
        assert sa._process is None
        assert sa._original_state is None
