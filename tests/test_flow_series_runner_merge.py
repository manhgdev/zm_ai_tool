"""Series automation merges each episode immediately after its final scene."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pipeline.flow.series_runner import SeriesRunner, _Run
from pipeline.flow import series as series_module


class SeriesRunnerMergeTests(unittest.TestCase):
    def test_last_completed_video_auto_merges_episode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "scene1.mp4"
            second = root / "scene2.mp4"
            first.touch()
            second.touch()
            snapshot = {
                "episodes": [{
                    "id": "ep1",
                    "scenes": [
                        {"status": "complete", "videoOutput": str(first)},
                        {"status": "complete", "videoOutput": str(second)},
                    ],
                }],
            }
            merged = root / "episode_merged.mp4"
            with patch.object(series_module, "_asset_folder", return_value=root), patch.object(
                series_module, "extract_video_end_frame", return_value=True
            ), patch.object(series_module, "update_scene"), patch.object(
                series_module, "get_series", return_value=snapshot
            ), patch.object(series_module, "merge_episode_videos", return_value=merged) as merge, patch.object(
                series_module, "update_episode"
            ) as update_episode:
                series_module.mark_job_complete({
                    "seriesContext": {
                        "artifact": "video", "seriesId": "series1",
                        "episodeId": "ep1", "sceneId": "scene2",
                    },
                }, [str(second)])

            merge.assert_called_once_with("series1", "ep1")
            update_episode.assert_called_once_with("series1", "ep1", {"mergedVideo": str(merged)})

    def test_keyframes_only_enqueues_first_scene_of_episode_only(self) -> None:
        runner = SeriesRunner()
        series = {
            "id": "series1",
            "episodes": [{
                "id": "ep1",
                "scenes": [
                    {"id": "scene1", "index": 1, "status": "draft"},
                    {"id": "scene2", "index": 2, "status": "draft"},
                ],
            }],
        }
        context = {"prompt": "first", "outputDir": "series/ep1", "sourceFiles": []}
        with patch("pipeline.flow.series.get_series", return_value=series), patch(
            "pipeline.flow.store.get_row", return_value={"id": "account1"}
        ), patch("pipeline.flow.series.generation_context", return_value=context) as generation_context, patch(
            "pipeline.flow.service.service.enqueue", return_value=[{"id": "job1"}]
        ) as enqueue, patch("pipeline.flow.series.register_job"):
            runner.start_run(
                series_id="series1",
                episode_ids=["ep1"],
                account_id="account1",
                settings={},
                mode="keyframes_only",
            )

        generation_context.assert_called_once_with("series1", "ep1", "scene1", "keyframe")
        enqueue.assert_called_once()

    def test_orchestrate_merges_at_each_episode_boundary(self) -> None:
        runner = SeriesRunner()
        run = _Run("run_test", 3)
        scenes = [
            ({"id": "ep1"}, {"id": "scene1"}),
            ({"id": "ep1"}, {"id": "scene2"}),
            ({"id": "ep2"}, {"id": "scene3"}),
        ]

        with patch.object(runner, "_process_scene"), patch(
            "pipeline.flow.series.merge_episode_videos"
        ) as merge:
            runner._orchestrate(run, "series1", scenes, "account1", {}, "image", True, "full")

        self.assertEqual(merge.call_args_list, [call("series1", "ep1"), call("series1", "ep2")])
        self.assertEqual(run.snapshot()["status"], "done")

    def test_orchestrate_reports_merge_failure(self) -> None:
        runner = SeriesRunner()
        run = _Run("run_test", 1)

        with patch.object(runner, "_process_scene"), patch(
            "pipeline.flow.series.merge_episode_videos", side_effect=RuntimeError("merge failed")
        ):
            runner._orchestrate(
                run,
                "series1",
                [({"id": "ep1"}, {"id": "scene1"})],
                "account1",
                {},
                "image",
                True,
                "full",
            )

        snapshot = run.snapshot()
        self.assertEqual(snapshot["status"], "done_with_errors")
        self.assertEqual(snapshot["errors"], [{"sceneId": "ep1", "error": "merge failed"}])


if __name__ == "__main__":
    unittest.main()
