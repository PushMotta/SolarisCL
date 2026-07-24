"""Tests for everything that does not need Houdini installed."""

import os
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hsl.cli import parse_frames
from hsl.husk import (
    FrameChunk, RenderJob, build_command, format_command, frame_chunks,
    jobs_for_rop, looks_like_error, parse_progress,
)
from hsl.manifest import (
    Camera, RenderProduct, RenderRop, RenderSettings, RenderVar, SceneManifest,
)
from hsl.runner import RenderQueue, State


def sample_manifest() -> SceneManifest:
    return SceneManifest(
        hip_path="/jobs/shot/shot_v012.hip",
        houdini_version="20.5.370",
        fps=25.0,
        default_settings_prim="/Render/rendersettings",
        rops=[RenderRop(
            node_path="/stage/usdrender_rop1",
            node_type="usdrender_rop",
            input_lop="/stage/karmarendersettings1",
            renderer="BRAY_HdKarmaXPU",
            settings_prim="/Render/rendersettings",
            camera="/cameras/shotcam",
            frame_start=1001, frame_end=1100, frame_inc=1,
            use_frame_range=True,
        )],
        settings=[RenderSettings(
            prim_path="/Render/rendersettings",
            resolution=(2048, 858),
            camera="/cameras/shotcam",
            products=["/Render/Products/beauty", "/Render/Products/crypto"],
            included_purposes=["render"],
            renderer_settings={"karma:global:samplesperpixel": 64},
        )],
        products=[
            RenderProduct(prim_path="/Render/Products/beauty",
                          product_name="/renders/shot_beauty.$F4.exr",
                          ordered_vars=["/Render/Vars/Ci", "/Render/Vars/N"]),
            RenderProduct(prim_path="/Render/Products/crypto",
                          product_name="/renders/shot_crypto.$F4.exr",
                          ordered_vars=["/Render/Vars/Ci"]),
        ],
        vars=[
            RenderVar(prim_path="/Render/Vars/Ci", source_name="Ci",
                      source_type="raw", data_type="color3f"),
            RenderVar(prim_path="/Render/Vars/N", source_name="N",
                      source_type="raw", data_type="normal3f"),
        ],
        cameras=[Camera(prim_path="/cameras/shotcam", focal_length=35.0)],
    )


class TestManifest(unittest.TestCase):
    def test_round_trip(self):
        original = sample_manifest()
        restored = SceneManifest.from_json(original.to_json())

        self.assertEqual(restored.hip_path, original.hip_path)
        self.assertIsInstance(restored.rops[0], RenderRop)
        self.assertIsInstance(restored.settings[0], RenderSettings)
        self.assertIsInstance(restored.cameras[0], Camera)
        self.assertEqual(restored.settings[0].resolution, (2048, 858))
        self.assertEqual(restored.rops[0].renderer, "BRAY_HdKarmaXPU")

    def test_frame_count(self):
        rop = sample_manifest().rops[0]
        self.assertEqual(rop.frame_count, 100)
        rop.frame_inc = 2
        self.assertEqual(rop.frame_count, 50)
        rop.use_frame_range = False
        self.assertEqual(rop.frame_count, 1)

    def test_resolve_settings_prefers_rop(self):
        m = sample_manifest()
        self.assertEqual(m.resolve_settings(m.rops[0]).prim_path,
                         "/Render/rendersettings")

    def test_resolve_settings_falls_back_to_stage_default(self):
        m = sample_manifest()
        m.rops[0].settings_prim = ""
        self.assertEqual(m.resolve_settings(m.rops[0]).prim_path,
                         "/Render/rendersettings")

    def test_outputs_and_aovs(self):
        m = sample_manifest()
        settings = m.settings[0]
        self.assertEqual(m.outputs_for(settings),
                         ["/renders/shot_beauty.$F4.exr",
                          "/renders/shot_crypto.$F4.exr"])
        # Ci appears in both products but must be deduplicated.
        self.assertEqual([v.label for v in m.aovs_for(settings)], ["Ci", "N"])


class TestChunking(unittest.TestCase):
    def test_single_chunk_by_default(self):
        chunks = frame_chunks(1001, 1100)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0], FrameChunk(1001, 100, 1))
        self.assertEqual(chunks[0].end, 1100)

    def test_even_split(self):
        chunks = frame_chunks(1, 100, 1, chunk_size=10)
        self.assertEqual(len(chunks), 10)
        self.assertEqual(chunks[0], FrameChunk(1, 10, 1))
        self.assertEqual(chunks[-1], FrameChunk(91, 10, 1))
        self.assertEqual(chunks[-1].end, 100)

    def test_ragged_split_covers_every_frame(self):
        chunks = frame_chunks(1, 95, 1, chunk_size=10)
        rendered = []
        for chunk in chunks:
            rendered += [chunk.start + i * chunk.inc for i in range(chunk.count)]
        self.assertEqual(rendered, list(range(1, 96)))

    def test_increment_is_respected(self):
        chunks = frame_chunks(1, 100, 2, chunk_size=10)
        rendered = []
        for chunk in chunks:
            rendered += [chunk.start + i * chunk.inc for i in range(chunk.count)]
        self.assertEqual(rendered, list(range(1, 100, 2)))
        self.assertEqual(len(rendered), 50)

    def test_chunk_bigger_than_range(self):
        self.assertEqual(len(frame_chunks(1, 5, 1, chunk_size=1000)), 1)

    def test_reversed_range_is_normalised(self):
        self.assertEqual(frame_chunks(100, 1)[0], FrameChunk(1, 100, 1))

    def test_label(self):
        self.assertEqual(str(FrameChunk(1001, 1, 1)), "1001")
        self.assertEqual(str(FrameChunk(1001, 10, 1)), "1001-1010")
        self.assertEqual(str(FrameChunk(1001, 10, 2)), "1001-1019x2")


class TestCommand(unittest.TestCase):
    def build(self, **kwargs):
        job = RenderJob(usd_file="/tmp/shot.usd", husk_exe="/opt/hfs/bin/husk",
                        **kwargs)
        return build_command(job)

    def test_minimal(self):
        cmd = self.build(renderer="BRAY_HdKarma", chunk=FrameChunk(1001, 50, 1))
        self.assertEqual(cmd[0], "/opt/hfs/bin/husk")
        self.assertEqual(cmd[-1], "/tmp/shot.usd", "USD file must come last")
        self.assertIn("--renderer", cmd)
        self.assertEqual(cmd[cmd.index("--frame") + 1], "1001")
        self.assertEqual(cmd[cmd.index("--frame-count") + 1], "50")
        self.assertNotIn("--frame-inc", cmd)

    def test_increment_emitted_only_when_needed(self):
        cmd = self.build(chunk=FrameChunk(1, 10, 3))
        self.assertEqual(cmd[cmd.index("--frame-inc") + 1], "3")

    def test_overrides(self):
        cmd = self.build(
            settings_prim="/Render/rs_beauty", camera="/cameras/shotcam",
            output="/renders/out.$F4.exr", resolution=(960, 540), threads=8,
            snapshot_interval=60,
        )
        self.assertEqual(cmd[cmd.index("--settings") + 1], "/Render/rs_beauty")
        self.assertEqual(cmd[cmd.index("--camera") + 1], "/cameras/shotcam")
        self.assertEqual(cmd[cmd.index("--output") + 1], "/renders/out.$F4.exr")
        res = cmd.index("--res")
        self.assertEqual(cmd[res + 1:res + 3], ["960", "540"])
        self.assertEqual(cmd[cmd.index("--threads") + 1], "8")
        self.assertEqual(cmd[cmd.index("--snapshot") + 1], "60")

    def test_empty_overrides_are_omitted(self):
        cmd = self.build()
        for flag in ("--settings", "--camera", "--output", "--res",
                     "--threads", "--snapshot"):
            self.assertNotIn(flag, cmd)

    def test_alfred_flag_appended_to_verbosity(self):
        cmd = self.build(verbosity="3", alfred_progress=True)
        self.assertEqual(cmd[cmd.index("--verbose") + 1], "3a")

    def test_alfred_not_duplicated(self):
        cmd = self.build(verbosity="9a", alfred_progress=True)
        self.assertEqual(cmd[cmd.index("--verbose") + 1], "9a")

    def test_alfred_can_be_disabled(self):
        cmd = self.build(verbosity="2", alfred_progress=False)
        self.assertEqual(cmd[cmd.index("--verbose") + 1], "2")

    def test_extra_args_precede_the_usd_file(self):
        cmd = self.build(extra_args=["--disable-motionblur"])
        self.assertEqual(cmd.index("--disable-motionblur"), len(cmd) - 2)

    def test_format_quotes_paths_with_spaces(self):
        text = format_command(["husk", "--output", "/my renders/a.exr", "/tmp/s.usd"])
        self.assertIn('"/my renders/a.exr"', text)
        self.assertNotIn('"husk"', text)


class TestJobsForRop(unittest.TestCase):
    def test_seeds_from_manifest(self):
        m = sample_manifest()
        jobs = jobs_for_rop(m, m.rops[0], "/tmp/shot.usd")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].renderer, "BRAY_HdKarmaXPU")
        self.assertEqual(jobs[0].camera, "/cameras/shotcam")
        self.assertEqual(jobs[0].settings_prim, "/Render/rendersettings")
        self.assertEqual(jobs[0].chunk, FrameChunk(1001, 100, 1))

    def test_overrides_win(self):
        m = sample_manifest()
        jobs = jobs_for_rop(m, m.rops[0], "/tmp/shot.usd",
                            renderer="BRAY_HdKarma", camera="/cameras/alt")
        self.assertEqual(jobs[0].renderer, "BRAY_HdKarma")
        self.assertEqual(jobs[0].camera, "/cameras/alt")

    def test_none_overrides_are_ignored(self):
        m = sample_manifest()
        jobs = jobs_for_rop(m, m.rops[0], "/tmp/shot.usd", renderer=None)
        self.assertEqual(jobs[0].renderer, "BRAY_HdKarmaXPU")

    def test_chunking_produces_multiple_jobs(self):
        m = sample_manifest()
        jobs = jobs_for_rop(m, m.rops[0], "/tmp/shot.usd", chunk_size=25)
        self.assertEqual(len(jobs), 4)
        self.assertEqual([j.chunk.start for j in jobs], [1001, 1026, 1051, 1076])

    def test_single_frame_rop(self):
        m = sample_manifest()
        m.rops[0].use_frame_range = False
        jobs = jobs_for_rop(m, m.rops[0], "/tmp/shot.usd", chunk_size=10)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].chunk.count, 1)


class TestOutputParsing(unittest.TestCase):
    def test_progress(self):
        self.assertEqual(parse_progress("ALF_PROGRESS 42%"), 42)
        self.assertEqual(parse_progress("  ALF_PROGRESS  100 %"), 100)
        self.assertIsNone(parse_progress("Rendering frame 1001"))

    def test_errors(self):
        self.assertTrue(looks_like_error("Error: unable to open stage"))
        self.assertTrue(looks_like_error("USD ERROR: No such file or directory"))
        self.assertFalse(looks_like_error("Rendering 1001 at 1920x1080"))


class TestFrameParsing(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(parse_frames("1001"), (1001, 1001, 1))
        self.assertEqual(parse_frames("1001-1100"), (1001, 1100, 1))
        self.assertEqual(parse_frames("1001-1100x2"), (1001, 1100, 2))
        self.assertEqual(parse_frames("1:24"), (1, 24, 1))

    def test_rejects_nonsense(self):
        with self.assertRaises(Exception):
            parse_frames("first-last")


class TestRenderQueue(unittest.TestCase):
    """Drive the queue against a fake husk so the plumbing is exercised."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.fake = os.path.join(self.dir, "fake_husk.py")
        with open(self.fake, "w") as fh:
            fh.write(
                "#!/usr/bin/env python3\n"
                "import sys, time\n"
                "args = sys.argv[1:]\n"
                "for pct in (0, 50, 100):\n"
                "    print('ALF_PROGRESS %d%%' % pct, flush=True)\n"
                "print('rendered ' + args[-1], flush=True)\n"
                "sys.exit(7 if '--make-it-fail' in args else 0)\n"
            )
        os.chmod(self.fake, os.stat(self.fake).st_mode | stat.S_IEXEC)

    def job(self, start, fail=False):
        return RenderJob(
            usd_file=os.path.join(self.dir, "shot.usd"),
            husk_exe=self.fake,
            chunk=FrameChunk(start, 2, 1),
            extra_args=["--make-it-fail"] if fail else [],
        )

    def _run(self, jobs, **kwargs):
        events = []
        queue = RenderQueue(jobs, on_event=lambda *a: events.append(a), **kwargs)
        queue.start(block=True)
        return queue, events

    def test_success(self):
        queue, events = self._run([self.job(1)])
        task = queue.tasks[0]
        self.assertEqual(task.state, State.DONE)
        self.assertEqual(task.returncode, 0)
        self.assertEqual(task.progress, 100)
        self.assertEqual(queue.progress, 100)
        self.assertTrue(any(e[0] == "task_progress" for e in events))
        self.assertEqual(events[-1][0], "queue_finished")

    def test_failure_is_recorded(self):
        queue, _ = self._run([self.job(1, fail=True)])
        self.assertEqual(queue.tasks[0].state, State.FAILED)
        self.assertEqual(queue.tasks[0].returncode, 7)

    def test_multiple_chunks_all_run(self):
        queue, _ = self._run([self.job(1), self.job(3), self.job(5)],
                             max_parallel=2)
        self.assertTrue(all(t.state is State.DONE for t in queue.tasks))
        self.assertTrue(queue.finished)

    def test_missing_executable_fails_cleanly(self):
        job = RenderJob(usd_file="/tmp/shot.usd",
                        husk_exe="/definitely/not/here/husk")
        queue, _ = self._run([job])
        self.assertEqual(queue.tasks[0].state, State.FAILED)
        self.assertTrue(any("Could not start husk" in line
                            for line in queue.tasks[0].log))

    def test_log_is_captured(self):
        queue, _ = self._run([self.job(1)])
        self.assertTrue(any("rendered" in line for line in queue.tasks[0].log))


if __name__ == "__main__":
    unittest.main(verbosity=2)
