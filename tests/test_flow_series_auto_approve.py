"""Series keyframes auto-approve when the Flow job completes."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pipeline.flow import series as series_mod


class SeriesAutoApproveTests(unittest.TestCase):
    def test_mark_job_complete_auto_approves_keyframe(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        still = tmp / "kf.png"
        still.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        job = {
            "seriesContext": {
                "seriesId": "s1",
                "episodeId": "e1",
                "sceneId": "c1",
                "artifact": "keyframe",
            }
        }
        with patch.object(series_mod, "update_scene") as update_scene, patch.object(
            series_mod, "approve_keyframe", return_value={"status": "ready_video"}
        ) as approve:
            series_mod.mark_job_complete(job, [str(still)])
        update_scene.assert_called_once()
        approve.assert_called_once_with("s1", "e1", "c1", str(still))
        self.assertEqual(update_scene.call_args.args[3]["status"], "awaiting_keyframe")


if __name__ == "__main__":
    unittest.main()
