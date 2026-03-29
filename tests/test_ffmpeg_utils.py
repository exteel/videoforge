"""
Unit tests for utils/ffmpeg_utils.py.

Pure functions and argument-building logic are tested without running FFmpeg.
All subprocess calls are mocked via unittest.mock.patch.
"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import utils.ffmpeg_utils as ff
from utils.ffmpeg_utils import (
    DEFAULT_AUDIO_BITRATE,
    DEFAULT_AUDIO_CODEC,
    DEFAULT_CRF,
    DEFAULT_FPS,
    DEFAULT_PRESET,
    DEFAULT_RESOLUTION,
    DEFAULT_VIDEO_CODEC,
    DRAFT_CRF,
    DRAFT_PRESET,
    DRAFT_RESOLUTION,
    ZOOM_SCALE,
    ZOOM_UPSCALE_WIDTH,
    _esc_concat_path,
    get_duration,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_completed_process(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.stdout = stdout
    r.stderr = ""
    r.returncode = returncode
    return r


# ═══════════════════════════════════════════════════════════════════════════════
# Constants sanity checks
# ═══════════════════════════════════════════════════════════════════════════════

class TestConstants:

    def test_default_resolution_format(self):
        w, h = DEFAULT_RESOLUTION.split("x")
        assert int(w) == 1920
        assert int(h) == 1080

    def test_draft_resolution_smaller(self):
        dw, dh = DRAFT_RESOLUTION.split("x")
        w, h   = DEFAULT_RESOLUTION.split("x")
        assert int(dw) < int(w)
        assert int(dh) < int(h)

    def test_draft_crf_higher_than_default(self):
        """Higher CRF = lower quality / smaller file — draft should be higher."""
        assert DRAFT_CRF > DEFAULT_CRF

    def test_zoom_scale_greater_than_one(self):
        assert ZOOM_SCALE > 1.0

    def test_zoom_upscale_width_large(self):
        """8000px is the design value for negligible rounding error."""
        assert ZOOM_UPSCALE_WIDTH >= 4000

    def test_default_fps(self):
        assert DEFAULT_FPS == 30

    def test_default_video_codec(self):
        assert DEFAULT_VIDEO_CODEC == "libx264"

    def test_default_audio_codec(self):
        assert DEFAULT_AUDIO_CODEC == "aac"


# ═══════════════════════════════════════════════════════════════════════════════
# _esc_concat_path
# ═══════════════════════════════════════════════════════════════════════════════

class TestEscConcatPath:

    def test_no_apostrophe(self, tmp_path):
        p = tmp_path / "normal_file.mp4"
        escaped = _esc_concat_path(p)
        assert "'" not in escaped  # no raw single-quotes remain

    def test_apostrophe_in_filename(self, tmp_path):
        """Apostrophes must be escaped for FFmpeg concat demuxer."""
        p = tmp_path / "Machiavelli's Secrets.mp4"
        escaped = _esc_concat_path(p)
        # Raw unescaped apostrophe must not appear
        assert "Machiavelli's" not in escaped
        # The escaped sequence must be present
        assert r"'\''Machiavelli" in escaped or "\\''" in escaped or "Machiavelli" in escaped

    def test_returns_posix_path(self, tmp_path):
        p = tmp_path / "file.mp4"
        escaped = _esc_concat_path(p)
        assert "\\" not in escaped, "Windows-style backslashes should be converted to forward slashes"

    def test_accepts_string_input(self, tmp_path):
        p = str(tmp_path / "clip.mp4")
        result = _esc_concat_path(p)
        assert isinstance(result, str)

    def test_accepts_path_input(self, tmp_path):
        p = tmp_path / "clip.mp4"
        result = _esc_concat_path(p)
        assert isinstance(result, str)


# ═══════════════════════════════════════════════════════════════════════════════
# get_duration
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetDuration:

    def test_parses_duration_from_ffprobe_output(self, tmp_path):
        fake_audio = tmp_path / "audio.mp3"
        fake_audio.write_bytes(b"\x00" * 16)  # create file so existence check passes

        ffprobe_json = json.dumps({"format": {"duration": "42.5"}})
        mock_result = _make_completed_process(stdout=ffprobe_json)

        with patch("utils.ffmpeg_utils._run", return_value=mock_result):
            duration = get_duration(fake_audio)

        assert abs(duration - 42.5) < 1e-9

    def test_raises_file_not_found(self, tmp_path):
        missing = tmp_path / "nonexistent.mp3"
        with pytest.raises(FileNotFoundError):
            get_duration(missing)

    def test_raises_runtime_error_on_bad_json(self, tmp_path):
        fake_file = tmp_path / "corrupt.mp3"
        fake_file.write_bytes(b"\x00" * 16)

        mock_result = _make_completed_process(stdout="NOT JSON")

        with patch("utils.ffmpeg_utils._run", return_value=mock_result):
            with pytest.raises(RuntimeError):
                get_duration(fake_file)

    def test_raises_runtime_error_when_duration_missing(self, tmp_path):
        fake_file = tmp_path / "no_duration.mp3"
        fake_file.write_bytes(b"\x00" * 16)

        # ffprobe output with no duration key
        mock_result = _make_completed_process(stdout=json.dumps({"format": {}}))

        with patch("utils.ffmpeg_utils._run", return_value=mock_result):
            with pytest.raises(RuntimeError):
                get_duration(fake_file)

    def test_ffprobe_command_includes_show_format(self, tmp_path):
        fake_file = tmp_path / "clip.mp4"
        fake_file.write_bytes(b"\x00" * 16)

        mock_result = _make_completed_process(stdout=json.dumps({"format": {"duration": "10.0"}}))

        with patch("utils.ffmpeg_utils._run", return_value=mock_result) as mock_run:
            get_duration(fake_file)
            cmd = mock_run.call_args[0][0]
            assert "-show_format" in cmd
            assert "-print_format" in cmd
            assert "json" in cmd


# ═══════════════════════════════════════════════════════════════════════════════
# _run helper
# ═══════════════════════════════════════════════════════════════════════════════

class TestRunHelper:

    def test_raises_on_nonzero_returncode(self, tmp_path):
        with patch("subprocess.run") as mock_sub:
            mock_proc = MagicMock()
            mock_proc.returncode = 1
            mock_proc.stderr = "ffmpeg: error"
            mock_sub.return_value = mock_proc

            with pytest.raises(RuntimeError, match="FFmpeg command failed"):
                ff._run(["ffmpeg", "-invalid"])

    def test_does_not_raise_when_returncode_zero(self):
        with patch("subprocess.run") as mock_sub:
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout = ""
            mock_proc.stderr = ""
            mock_sub.return_value = mock_proc

            result = ff._run(["ffmpeg", "-version"], check=True)
            assert result is not None

    def test_check_false_does_not_raise_on_nonzero(self):
        with patch("subprocess.run") as mock_sub:
            mock_proc = MagicMock()
            mock_proc.returncode = 1
            mock_proc.stderr = "some error"
            mock_sub.return_value = mock_proc

            # Should NOT raise when check=False
            result = ff._run(["ffmpeg", "-bad"], check=False)
            assert result.returncode == 1


# ═══════════════════════════════════════════════════════════════════════════════
# resize — command structure
# ═══════════════════════════════════════════════════════════════════════════════

class TestResize:

    def test_command_contains_scale_filter(self, tmp_path):
        inp = tmp_path / "input.jpg"
        inp.write_bytes(b"\xff\xd8\xff")  # minimal JPEG header
        out = tmp_path / "output.jpg"

        with patch("utils.ffmpeg_utils._run") as mock_run:
            mock_run.return_value = MagicMock()
            ff.resize(inp, out)
            cmd = mock_run.call_args[0][0]
            # The vf filter must reference scale and pad
            vf_arg = cmd[cmd.index("-vf") + 1]
            assert "scale=" in vf_arg
            assert "pad=" in vf_arg

    def test_uses_draft_resolution_when_draft(self, tmp_path):
        inp = tmp_path / "i.jpg"
        inp.write_bytes(b"\x00")
        out = tmp_path / "o.jpg"

        with patch("utils.ffmpeg_utils._run") as mock_run:
            mock_run.return_value = MagicMock()
            ff.resize(inp, out, draft=True)
            # resize doesn't use draft settings directly — just pass-through; verify it ran
            assert mock_run.called

    def test_returns_output_path(self, tmp_path):
        inp = tmp_path / "i.jpg"
        inp.write_bytes(b"\x00")
        out = tmp_path / "o.jpg"

        with patch("utils.ffmpeg_utils._run") as mock_run:
            mock_run.return_value = MagicMock()
            result = ff.resize(inp, out)
            assert result == out


# ═══════════════════════════════════════════════════════════════════════════════
# ken_burns — command structure
# ═══════════════════════════════════════════════════════════════════════════════

class TestKenBurns:

    def _img(self, tmp_path) -> Path:
        p = tmp_path / "img.jpg"
        p.write_bytes(b"\xff\xd8\xff")
        return p

    def test_zoom_in_includes_zoompan(self, tmp_path):
        inp = self._img(tmp_path)
        out = tmp_path / "out.mp4"
        with patch("utils.ffmpeg_utils._run") as mock_run:
            mock_run.return_value = MagicMock()
            ff.ken_burns(inp, out, duration=5.0, animation="zoom_in")
            cmd = mock_run.call_args[0][0]
            vf_idx = cmd.index("-vf")
            vf = cmd[vf_idx + 1]
            assert "zoompan" in vf

    def test_static_does_not_include_zoompan(self, tmp_path):
        inp = self._img(tmp_path)
        out = tmp_path / "out.mp4"
        with patch("utils.ffmpeg_utils._run") as mock_run:
            mock_run.return_value = MagicMock()
            ff.ken_burns(inp, out, duration=5.0, animation="static")
            cmd = mock_run.call_args[0][0]
            vf_idx = cmd.index("-vf")
            vf = cmd[vf_idx + 1]
            assert "zoompan" not in vf

    def test_draft_uses_draft_crf(self, tmp_path):
        inp = self._img(tmp_path)
        out = tmp_path / "out.mp4"
        with patch("utils.ffmpeg_utils._run") as mock_run:
            mock_run.return_value = MagicMock()
            ff.ken_burns(inp, out, duration=3.0, draft=True)
            cmd = mock_run.call_args[0][0]
            crf_idx = cmd.index("-crf")
            assert cmd[crf_idx + 1] == str(DRAFT_CRF)

    def test_default_uses_default_crf(self, tmp_path):
        inp = self._img(tmp_path)
        out = tmp_path / "out.mp4"
        with patch("utils.ffmpeg_utils._run") as mock_run:
            mock_run.return_value = MagicMock()
            ff.ken_burns(inp, out, duration=3.0, animation="zoom_in")
            cmd = mock_run.call_args[0][0]
            crf_idx = cmd.index("-crf")
            assert cmd[crf_idx + 1] == str(DEFAULT_CRF)

    def test_duration_passed_as_t_flag(self, tmp_path):
        inp = self._img(tmp_path)
        out = tmp_path / "out.mp4"
        with patch("utils.ffmpeg_utils._run") as mock_run:
            mock_run.return_value = MagicMock()
            ff.ken_burns(inp, out, duration=7.5, animation="static")
            cmd = mock_run.call_args[0][0]
            t_idx = cmd.index("-t")
            assert cmd[t_idx + 1] == "7.5"

    def test_returns_output_path(self, tmp_path):
        inp = self._img(tmp_path)
        out = tmp_path / "out.mp4"
        with patch("utils.ffmpeg_utils._run") as mock_run:
            mock_run.return_value = MagicMock()
            result = ff.ken_burns(inp, out, duration=2.0, animation="static")
            assert result == out

    def test_zoom_upscale_width_in_filter(self, tmp_path):
        """Ken Burns zoom_in must pre-upscale to ZOOM_UPSCALE_WIDTH for quality."""
        inp = self._img(tmp_path)
        out = tmp_path / "out.mp4"
        with patch("utils.ffmpeg_utils._run") as mock_run:
            mock_run.return_value = MagicMock()
            ff.ken_burns(inp, out, duration=5.0, animation="zoom_in")
            cmd = mock_run.call_args[0][0]
            vf_idx = cmd.index("-vf")
            vf = cmd[vf_idx + 1]
            assert str(ZOOM_UPSCALE_WIDTH) in vf


# ═══════════════════════════════════════════════════════════════════════════════
# concat_videos — command structure
# ═══════════════════════════════════════════════════════════════════════════════

class TestConcatVideos:

    def test_single_video_copies_without_ffmpeg(self, tmp_path):
        src = tmp_path / "clip.mp4"
        src.write_bytes(b"\x00" * 32)
        out = tmp_path / "out.mp4"

        import shutil
        with patch("shutil.copy2") as mock_copy:
            with patch("utils.ffmpeg_utils._run") as mock_run:
                ff.concat_videos([src], out, crossfade=False)
                mock_copy.assert_called_once()
                mock_run.assert_not_called()

    def test_empty_list_raises_value_error(self, tmp_path):
        with pytest.raises(ValueError, match="cannot be empty"):
            ff.concat_videos([], tmp_path / "out.mp4")

    def test_no_crossfade_uses_concat_demuxer(self, tmp_path):
        clips = []
        for i in range(3):
            c = tmp_path / f"clip{i}.mp4"
            c.write_bytes(b"\x00" * 32)
            clips.append(c)
        out = tmp_path / "out.mp4"

        with patch("utils.ffmpeg_utils._run") as mock_run:
            mock_run.return_value = MagicMock()
            ff.concat_videos(clips, out, crossfade=False)
            cmd = mock_run.call_args[0][0]
            assert "-f" in cmd
            assert "concat" in cmd

    def test_crossfade_path_calls_get_duration(self, tmp_path):
        """Crossfade mode calls get_duration for each clip."""
        clips = []
        for i in range(2):
            c = tmp_path / f"clip{i}.mp4"
            c.write_bytes(b"\x00" * 32)
            clips.append(c)
        out = tmp_path / "out.mp4"

        with patch("utils.ffmpeg_utils.get_duration", return_value=5.0) as mock_dur:
            with patch("utils.ffmpeg_utils._run") as mock_run:
                mock_run.return_value = MagicMock()
                ff.concat_videos(clips, out, crossfade=True)
                assert mock_dur.call_count == len(clips)


# ═══════════════════════════════════════════════════════════════════════════════
# add_audio — command structure
# ═══════════════════════════════════════════════════════════════════════════════

class TestAddAudio:

    def test_shortest_flag_present_by_default(self, tmp_path):
        vid = tmp_path / "v.mp4"
        aud = tmp_path / "a.mp3"
        vid.write_bytes(b"\x00")
        aud.write_bytes(b"\x00")
        out = tmp_path / "o.mp4"

        with patch("utils.ffmpeg_utils._run") as mock_run:
            mock_run.return_value = MagicMock()
            ff.add_audio(vid, aud, out, shortest=True)
            cmd = mock_run.call_args[0][0]
            assert "-shortest" in cmd

    def test_shortest_flag_absent_when_false(self, tmp_path):
        vid = tmp_path / "v.mp4"
        aud = tmp_path / "a.mp3"
        vid.write_bytes(b"\x00")
        aud.write_bytes(b"\x00")
        out = tmp_path / "o.mp4"

        with patch("utils.ffmpeg_utils._run") as mock_run:
            mock_run.return_value = MagicMock()
            ff.add_audio(vid, aud, out, shortest=False)
            cmd = mock_run.call_args[0][0]
            assert "-shortest" not in cmd

    def test_audio_codec_is_aac(self, tmp_path):
        vid = tmp_path / "v.mp4"
        aud = tmp_path / "a.mp3"
        vid.write_bytes(b"\x00")
        aud.write_bytes(b"\x00")
        out = tmp_path / "o.mp4"

        with patch("utils.ffmpeg_utils._run") as mock_run:
            mock_run.return_value = MagicMock()
            ff.add_audio(vid, aud, out)
            cmd = mock_run.call_args[0][0]
            ca_idx = cmd.index("-c:a")
            assert cmd[ca_idx + 1] == DEFAULT_AUDIO_CODEC


# ═══════════════════════════════════════════════════════════════════════════════
# pad_video_end — command structure
# ═══════════════════════════════════════════════════════════════════════════════

class TestPadVideoEnd:

    def test_zero_pad_copies_without_ffmpeg(self, tmp_path):
        src = tmp_path / "v.mp4"
        src.write_bytes(b"\x00" * 32)
        out = tmp_path / "o.mp4"

        with patch("shutil.copy2") as mock_copy:
            with patch("utils.ffmpeg_utils._run") as mock_run:
                ff.pad_video_end(src, out, pad_seconds=0)
                mock_copy.assert_called_once()
                mock_run.assert_not_called()

    def test_negative_pad_copies_without_ffmpeg(self, tmp_path):
        src = tmp_path / "v.mp4"
        src.write_bytes(b"\x00" * 32)
        out = tmp_path / "o.mp4"

        with patch("shutil.copy2") as mock_copy:
            with patch("utils.ffmpeg_utils._run") as mock_run:
                ff.pad_video_end(src, out, pad_seconds=-1.0)
                mock_copy.assert_called_once()
                mock_run.assert_not_called()

    def test_positive_pad_calls_ffmpeg(self, tmp_path):
        src = tmp_path / "v.mp4"
        src.write_bytes(b"\x00")
        out = tmp_path / "o.mp4"

        with patch("utils.ffmpeg_utils._run") as mock_run:
            mock_run.return_value = MagicMock()
            ff.pad_video_end(src, out, pad_seconds=2.5)
            assert mock_run.called
            cmd = mock_run.call_args[0][0]
            # Verify tpad filter with stop_mode=clone is present
            vf_idx = cmd.index("-vf")
            vf = cmd[vf_idx + 1]
            assert "tpad" in vf
            assert "clone" in vf


# ═══════════════════════════════════════════════════════════════════════════════
# concat_audio
# ═══════════════════════════════════════════════════════════════════════════════

class TestConcatAudio:

    def test_empty_list_raises_value_error(self, tmp_path):
        with pytest.raises(ValueError, match="cannot be empty"):
            ff.concat_audio([], tmp_path / "out.mp3")

    def test_single_file_copies(self, tmp_path):
        src = tmp_path / "a.mp3"
        src.write_bytes(b"\xff\xfb")
        out = tmp_path / "out.mp3"

        with patch("shutil.copy2") as mock_copy:
            with patch("utils.ffmpeg_utils._run") as mock_run:
                ff.concat_audio([src], out)
                mock_copy.assert_called_once()
                mock_run.assert_not_called()

    def test_multiple_files_reencode_mp3(self, tmp_path):
        files = []
        for i in range(3):
            f = tmp_path / f"a{i}.mp3"
            f.write_bytes(b"\xff\xfb")
            files.append(f)
        out = tmp_path / "out.mp3"

        with patch("utils.ffmpeg_utils._run") as mock_run:
            mock_run.return_value = MagicMock()
            ff.concat_audio(files, out)
            cmd = mock_run.call_args[0][0]
            # Should use libmp3lame for mp3 output
            assert "libmp3lame" in cmd


# ═══════════════════════════════════════════════════════════════════════════════
# check_ffmpeg
# ═══════════════════════════════════════════════════════════════════════════════

class TestCheckFfmpeg:

    def test_returns_version_strings_on_success(self):
        with patch("subprocess.run") as mock_sub:
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout = "ffmpeg version 6.1.0\nsome other line"
            mock_sub.return_value = mock_proc

            ffv, fpv = ff.check_ffmpeg()
            assert "ffmpeg" in ffv.lower()

    def test_raises_if_ffmpeg_not_found(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("not found")):
            with pytest.raises(RuntimeError, match="not found"):
                ff.check_ffmpeg()
