"""Tests for darktable_mcp/tools/segmentation_tools.py's error routing --
specifically the 2026-07-27 Grounded-SAM addition: LabelNotFoundInImageError
must propagate as-is, never trigger the GrabCut fallback (GrabCut has no
text grounding either -- see local_segment._label_only_rect_px -- so
"falling back" would silently swap a confident rejection for a low-quality
generic-rect guess). A plain SegmentationServiceError (the existing,
pre-2026-07-27 case: sidecar down/crashed/timed out) must still fall back
as before -- this suite guards both directions so the new special case
doesn't regress the old default behavior.
"""

from unittest.mock import Mock, patch

import pytest

from darktable_mcp.tools import segmentation_tools
from darktable_mcp.utils.errors import LabelNotFoundInImageError, SegmentationServiceError


def _completed_process(returncode=0, stdout="", stderr=""):
    proc = Mock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


class TestResolveDefaultCheckpoint:
    """Regression guard for the 2026-07-27 checkpoint-bump-broke-existing-
    installs incident: bumping the *default* checkpoint filename (tiny ->
    small) pointed every pre-existing install-sidecar checkout (which only
    ever downloaded tiny) at a file that doesn't exist there, silently
    downgrading them to the GrabCut fallback with no error surfaced."""

    def test_prefers_small_when_present(self, tmp_path):
        (tmp_path / "checkpoints").mkdir()
        (tmp_path / "checkpoints" / "sam2.1_hiera_small.pt").write_text("fake")
        ckpt, cfg = segmentation_tools._resolve_default_checkpoint(tmp_path)
        assert ckpt.name == "sam2.1_hiera_small.pt"
        assert cfg == "configs/sam2.1/sam2.1_hiera_s.yaml"

    def test_falls_back_to_tiny_when_only_tiny_present(self, tmp_path):
        (tmp_path / "checkpoints").mkdir()
        (tmp_path / "checkpoints" / "sam2.1_hiera_tiny.pt").write_text("fake")
        ckpt, cfg = segmentation_tools._resolve_default_checkpoint(tmp_path)
        assert ckpt.name == "sam2.1_hiera_tiny.pt"
        assert cfg == "configs/sam2.1/sam2.1_hiera_t.yaml"

    def test_prefers_small_over_tiny_when_both_present(self, tmp_path):
        (tmp_path / "checkpoints").mkdir()
        (tmp_path / "checkpoints" / "sam2.1_hiera_small.pt").write_text("fake")
        (tmp_path / "checkpoints" / "sam2.1_hiera_tiny.pt").write_text("fake")
        ckpt, _cfg = segmentation_tools._resolve_default_checkpoint(tmp_path)
        assert ckpt.name == "sam2.1_hiera_small.pt"

    def test_defaults_to_small_when_neither_present(self, tmp_path):
        """Fresh install, nothing downloaded yet -- error message should
        still point at the modern (small) filename, not silently pick a
        nonexistent tiny path either."""
        ckpt, cfg = segmentation_tools._resolve_default_checkpoint(tmp_path)
        assert ckpt.name == "sam2.1_hiera_small.pt"
        assert cfg == "configs/sam2.1/sam2.1_hiera_s.yaml"


class TestRunSidecarLabelNotFound:
    @patch("darktable_mcp.tools.segmentation_tools.subprocess.run")
    @patch("darktable_mcp.tools.segmentation_tools._sidecar_script")
    @patch("darktable_mcp.tools.segmentation_tools._sidecar_python")
    def test_run_sidecar_raises_label_not_found_on_error_type_marker(
        self, mock_python, mock_script, mock_run, tmp_path
    ):
        python = tmp_path / "python3.12"
        python.write_text("")
        script = tmp_path / "segment.py"
        script.write_text("")
        mock_python.return_value = python
        mock_script.return_value = script
        mock_run.return_value = _completed_process(
            returncode=0,
            stdout='{"error": "label \'a helicopter\' not found", "error_type": "label_not_found"}',
        )

        with pytest.raises(LabelNotFoundInImageError, match="a helicopter"):
            segmentation_tools._run_sidecar("image.png", label="a helicopter")

    @patch("darktable_mcp.tools.segmentation_tools.subprocess.run")
    @patch("darktable_mcp.tools.segmentation_tools._sidecar_script")
    @patch("darktable_mcp.tools.segmentation_tools._sidecar_python")
    def test_run_sidecar_normal_success_unaffected(
        self, mock_python, mock_script, mock_run, tmp_path
    ):
        python = tmp_path / "python3.12"
        python.write_text("")
        script = tmp_path / "segment.py"
        script.write_text("")
        mock_python.return_value = python
        mock_script.return_value = script
        mock_run.return_value = _completed_process(
            returncode=0,
            stdout=(
                '{"polygon": [{"x": 0.1, "y": 0.1}], "bbox": {}, "score": 0.9, "backend": "sam2"}'
            ),
        )

        result = segmentation_tools._run_sidecar("image.png", label="a face")
        assert result["backend"] == "sam2"


class TestRunSegmentationFallbackRouting:
    @patch("darktable_mcp.tools.segmentation_tools.local_segment.segment_grabcut")
    @patch("darktable_mcp.tools.segmentation_tools._run_sidecar")
    def test_label_not_found_never_falls_back_to_grabcut(self, mock_sidecar, mock_grabcut):
        mock_sidecar.side_effect = LabelNotFoundInImageError("label 'a helicopter' not found")

        with pytest.raises(LabelNotFoundInImageError):
            segmentation_tools.run_segmentation("image.png", label="a helicopter")

        mock_grabcut.assert_not_called()

    @patch("darktable_mcp.tools.segmentation_tools.local_segment.segment_grabcut")
    @patch("darktable_mcp.tools.segmentation_tools._run_sidecar")
    def test_plain_sidecar_error_still_falls_back_to_grabcut(self, mock_sidecar, mock_grabcut):
        """Regression guard: the new label_not_found special-case must not
        break the EXISTING fallback behavior for an ordinary sidecar
        failure (venv missing, crashed, timed out, etc.)."""
        mock_sidecar.side_effect = SegmentationServiceError("sidecar exited with code 1")
        mock_grabcut.return_value = {"polygon": [{"x": 0.1, "y": 0.1}], "backend": "grabcut"}

        result = segmentation_tools.run_segmentation(
            "image.png", box={"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}
        )

        assert result["backend"] == "grabcut"
        mock_grabcut.assert_called_once()
