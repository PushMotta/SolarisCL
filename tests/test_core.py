"""Tests for everything that does not need Houdini installed."""

import contextlib
import io
import json
import os
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hsl import (
    bridge, cli as cli_mod, farm, husk as husk_mod, inspector, preflight,
    presets, resources,
)
from hsl import memlog, sysinfo
from hsl.bridge import (
    find_hython, list_hython_installations, load_user_settings, save_user_setting,
)
from hsl.cli import (
    _resolve_hython_choice, build_parser, cmd_cook, cmd_inspect, cmd_render,
    parse_frames,
)
from hsl.husk import (
    DEFAULT_ENGINE, FrameChunk, RenderJob, build_command, chunks_for_task,
    expand_frame_token, format_command, frame_chunks, has_unexpanded_tokens,
    jobs_for_rop, jobs_for_task, looks_like_error, parse_progress,
)
from hsl.manifest import (
    TASK_CACHE, TASK_RENDER, TASK_SIM, TASK_UNKNOWN, Camera, LiveVolume,
    MissingAsset, OutputTask, RenderProduct, RenderRop, RenderSettings,
    RenderVar, SceneManifest,
)
from hsl.progress import (
    eta_seconds, format_eta, queue_progress_summary, task_progress_text,
)
from hsl.runner import RenderQueue, State, Task

_MEMLOG_REDIRECT = None


def setUpModule():
    """Keep the whole module off the real render-memory history.

    The fake-husk tests run genuine subprocesses, so the queue genuinely
    measures them and genuinely files the result -- straight into the user's
    profile. A test run wrote 212 junk samples there before this existed, which
    would have diluted the very history preflight reads to warn people. Point
    the store at a temp directory for every test in this file, not just the
    ones that know about it.
    """
    global _MEMLOG_REDIRECT
    _MEMLOG_REDIRECT = (tempfile.TemporaryDirectory(prefix="hsl_memlog_"),
                        memlog.LOG_FILE)
    memlog.LOG_FILE = os.path.join(_MEMLOG_REDIRECT[0].name, "memory.json")


def tearDownModule():
    tmpdir, original = _MEMLOG_REDIRECT
    memlog.LOG_FILE = original
    tmpdir.cleanup()


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

    def test_round_trip_live_volumes(self):
        original = sample_manifest()
        original.live_volumes = [
            LiveVolume(prim_path="/stage/pyro/volume",
                       field_count=2, field_names=["density", "vel"]),
        ]
        restored = SceneManifest.from_json(original.to_json())
        self.assertIsInstance(restored.live_volumes[0], LiveVolume)
        self.assertEqual(restored.live_volumes[0].field_names, ["density", "vel"])
        self.assertEqual(restored.live_volumes[0].field_count, 2)
        self.assertEqual(restored.live_volumes[0].label, "volume")

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
        # These assert on husk argv, so pin the engine: the app-wide default is
        # now hython, which builds an entirely different command.
        kwargs.setdefault("engine", "husk")
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

    def test_hython_engine_command(self):
        job = RenderJob(engine="hython", hip_file="/jobs/shot.hip", rop_path="/stage/usdrender1",
                        chunk=FrameChunk(1001, 10, 1), hython_exe="/opt/hfs/bin/hython")
        cmd = build_command(job)
        self.assertEqual(cmd[0], "/opt/hfs/bin/hython")
        self.assertEqual(cmd[1:4], ["-m", "hsl.inspector", "/jobs/shot.hip"])
        self.assertIn("--render-direct", cmd)
        self.assertEqual(cmd[cmd.index("--rop") + 1], "/stage/usdrender1")
        self.assertEqual(cmd[cmd.index("--frame-start") + 1], "1001")
        self.assertEqual(cmd[cmd.index("--frame-count") + 1], "10")


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
        if os.name == "nt":
            self.fake_exe = os.path.join(self.dir, "fake_husk.bat")
            with open(self.fake_exe, "w") as fh:
                fh.write(f'@echo off\n"{sys.executable}" "{self.fake}" %*\n')
        else:
            self.fake_exe = self.fake

    def job(self, start, fail=False):
        return RenderJob(
            usd_file=os.path.join(self.dir, "shot.usd"),
            engine="husk",              # the fake executable is a husk stand-in
            husk_exe=self.fake_exe,
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
        job = RenderJob(usd_file="/tmp/shot.usd", engine="husk",
                        husk_exe="/definitely/not/here/husk")
        queue, _ = self._run([job])
        self.assertEqual(queue.tasks[0].state, State.FAILED)
        self.assertTrue(any("Could not start husk" in line
                            for line in queue.tasks[0].log))

    def test_log_is_captured(self):
        queue, _ = self._run([self.job(1)])
        self.assertTrue(any("rendered" in line for line in queue.tasks[0].log))


class TestOutputVerification(unittest.TestCase):
    """T6: a husk process can exit 0 having written nothing."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.fake = os.path.join(self.dir, "fake_husk.py")
        with open(self.fake, "w") as fh:
            fh.write("import sys\nprint('ALF_PROGRESS 100%', flush=True)\nsys.exit(0)\n")
        if os.name == "nt":
            self.exe = os.path.join(self.dir, "fake_husk.bat")
            with open(self.exe, "w") as fh:
                fh.write(f'@echo off\n"{sys.executable}" "{self.fake}" %*\n')
        else:
            self.exe = self.fake
            os.chmod(self.fake, os.stat(self.fake).st_mode | stat.S_IEXEC)

    def _run(self, expected):
        job = RenderJob(usd_file=os.path.join(self.dir, "shot.usd"),
                        engine="husk", husk_exe=self.exe,
                        chunk=FrameChunk(1, 2, 1), expected_outputs=expected)
        queue = RenderQueue([job])
        queue.start(block=True)
        return queue.tasks[0]

    def test_exit_zero_with_no_output_is_a_failure(self):
        task = self._run([os.path.join(self.dir, "out.$F4.exr")])
        self.assertEqual(task.returncode, 0)
        self.assertIs(task.state, State.FAILED)
        self.assertTrue(any("wrote none of its" in line for line in task.log))

    def test_outputs_present_means_done(self):
        for frame in (1, 2):
            with open(os.path.join(self.dir, "out.%04d.exr" % frame), "w") as fh:
                fh.write("pixels")
        task = self._run([os.path.join(self.dir, "out.$F4.exr")])
        self.assertIs(task.state, State.DONE)

    def test_empty_file_does_not_count_as_output(self):
        for frame in (1, 2):
            open(os.path.join(self.dir, "out.%04d.exr" % frame), "w").close()
        task = self._run([os.path.join(self.dir, "out.$F4.exr")])
        self.assertIs(task.state, State.FAILED)

    def test_partial_output_still_passes_but_warns(self):
        # Only frame 1 written: a partial miss must not fail a good render.
        with open(os.path.join(self.dir, "out.0001.exr"), "w") as fh:
            fh.write("pixels")
        task = self._run([os.path.join(self.dir, "out.$F4.exr")])
        self.assertIs(task.state, State.DONE)
        self.assertTrue(any("1 of 2 expected" in line for line in task.log))

    def test_unknown_expectations_are_not_guessed(self):
        self.assertIs(self._run([]).state, State.DONE)

    def test_printf_named_outputs_are_verified(self):
        for frame in (1, 2):
            with open(os.path.join(self.dir, "out.%04d.exr" % frame), "w") as fh:
                fh.write("pixels")
        task = self._run([os.path.join(self.dir, "out.%04d.exr")])
        self.assertIs(task.state, State.DONE)

    def test_unresolvable_token_does_not_fail_a_good_render(self):
        # Regression: $FF cannot be expanded, so the path could never be found
        # and the check marked a perfectly good render FAILED.
        task = self._run([os.path.join(self.dir, "out.$FF.exr")])
        self.assertIs(task.state, State.DONE)
        self.assertTrue(any("unresolved token" in line for line in task.log))


class TestFrameTokens(unittest.TestCase):
    """husk expands $F/$FF/$F4, $N, <F>/<F4> and %d/%g/%04d (from --help)."""

    def test_dollar_f_padding(self):
        self.assertEqual(expand_frame_token("/r/shot.$F4.exr", 7), "/r/shot.0007.exr")
        self.assertEqual(expand_frame_token("/r/shot.$F.exr", 7), "/r/shot.7.exr")
        self.assertEqual(expand_frame_token("/r/shot.${F4}.exr", 12), "/r/shot.0012.exr")

    def test_udim_style(self):
        self.assertEqual(expand_frame_token("/r/shot.<F4>.exr", 7), "/r/shot.0007.exr")
        self.assertEqual(expand_frame_token("/r/shot.<F>.exr", 7), "/r/shot.7.exr")

    def test_printf_style(self):
        self.assertEqual(expand_frame_token("/r/shot.%04d.exr", 7), "/r/shot.0007.exr")
        self.assertEqual(expand_frame_token("/r/shot.%d.exr", 7), "/r/shot.7.exr")

    def test_sequence_token_needs_an_index(self):
        self.assertEqual(expand_frame_token("/r/shot.$N.exr", 1007, 3), "/r/shot.3.exr")
        # Without an index it is left alone rather than confused with the frame.
        self.assertTrue(has_unexpanded_tokens(expand_frame_token("/r/shot.$N.exr", 1007)))

    def test_ambiguous_tokens_are_left_alone_not_guessed(self):
        # $FF and %g are float forms whose spelling is unconfirmed. Expanding
        # them by guesswork would invent a filename no render ever writes.
        for template in ("/r/shot.$FF.exr", "/r/shot.%g.exr"):
            self.assertTrue(has_unexpanded_tokens(expand_frame_token(template, 7)),
                            f"{template} should be reported as unresolved")
        self.assertNotIn("7F", expand_frame_token("/r/shot.$FF.exr", 7))

    def test_path_without_a_token_is_unchanged_and_resolved(self):
        self.assertEqual(expand_frame_token("/r/single.exr", 7), "/r/single.exr")
        self.assertFalse(has_unexpanded_tokens("/r/single.exr"))


class TestHythonDiscovery(unittest.TestCase):
    def test_list_hython_installations(self):
        installs = list_hython_installations()
        self.assertIsInstance(installs, list)
        for label, path in installs:
            self.assertTrue(os.path.isfile(path))

    def test_find_hython_explicit(self):
        fake_file = tempfile.NamedTemporaryFile(delete=False)
        fake_file.close()
        try:
            found = find_hython(fake_file.name)
            self.assertEqual(os.path.abspath(found), os.path.abspath(fake_file.name))
        finally:
            if os.path.exists(fake_file.name):
                os.remove(fake_file.name)


class TestAovSelection(unittest.TestCase):
    """AOV editing is a USD edit, not a husk flag. Test the plain-Python half:
    resolving an --aovs spec to RenderVar prim paths, and that no husk AOV flag
    is ever emitted. The USD overlay itself needs hython and is checked there."""

    def test_no_aov_flag_is_ever_emitted(self):
        # husk has no --aov/--skip-aov; build_command must not invent one.
        cmd = build_command(RenderJob(usd_file="/jobs/shot.usd", renderer="BRAY_HdKarma"))
        self.assertNotIn("--aov", cmd)
        self.assertNotIn("--skip-aov", cmd)

    def test_resolve_aovs_by_name(self):
        from hsl.cli import _resolve_aovs
        m = sample_manifest()   # vars: /Render/Vars/Ci (Ci), /Render/Vars/N (N)
        self.assertEqual(_resolve_aovs(m, "Ci"), ["/Render/Vars/Ci"])
        self.assertEqual(set(_resolve_aovs(m, "ci,n")),
                         {"/Render/Vars/Ci", "/Render/Vars/N"})

    def test_resolve_aovs_unknown_is_empty(self):
        from hsl.cli import _resolve_aovs
        self.assertEqual(_resolve_aovs(sample_manifest(), "does_not_exist"), [])


class TestPreflight(unittest.TestCase):
    def test_preflight_resolution_warning(self):
        job = RenderJob(usd_file="/jobs/shot.usd", resolution=(3840, 2160))
        warnings = preflight.run_preflight_checks(job)
        self.assertTrue(any(w.category == "resolution" for w in warnings))

    def test_preflight_clean(self):
        job = RenderJob(usd_file="/jobs/shot.usd", resolution=(1920, 1080))
        warnings = preflight.run_preflight_checks(job)
        self.assertFalse(any(w.level == "error" for w in warnings))

    def test_preflight_flags_missing_textures(self):
        # The gap this closes: a missing texture must be a preflight error.
        m = sample_manifest()
        m.missing_assets = [MissingAsset(attr_path="/mat/tex.inputs:file",
                                         asset_path="/tex/wood.exr")]
        warnings = preflight.run_preflight_checks(RenderJob(usd_file="/s.usd"), m)
        hits = [w for w in warnings if w.category == "missing_asset"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].level, "error")
        self.assertIn("wood.exr", hits[0].message)

    def test_tokenless_output_over_a_sequence_warns(self):
        job = RenderJob(usd_file="/s.usd", output="/renders/hero.exr",
                        chunk=FrameChunk(1, 30, 1))
        hits = [w for w in preflight.run_preflight_checks(job)
                if w.category == "output_path" and "overwrite" in w.message]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].level, "warning")

    def test_tokenised_output_does_not_warn(self):
        job = RenderJob(usd_file="/s.usd", output="/renders/hero.$F4.exr",
                        chunk=FrameChunk(1, 30, 1))
        self.assertFalse(any("overwrite" in w.message
                             for w in preflight.run_preflight_checks(job)))

    def test_single_frame_tokenless_output_is_fine(self):
        job = RenderJob(usd_file="/s.usd", output="/renders/hero.exr",
                        chunk=FrameChunk(1, 1, 1))
        self.assertFalse(any("overwrite" in w.message
                             for w in preflight.run_preflight_checks(job)))

    def test_scheduled_relink_downgrades_missing_assets(self):
        # A hython job repaths at render time, so the manifest still lists the
        # assets as unresolved here. Erroring would refuse to start the very
        # render that fixes them.
        m = sample_manifest()
        m.missing_assets = [MissingAsset(attr_path="/mat/tex.inputs:file",
                                         asset_path="/tex/wood.exr")]
        job = RenderJob(usd_file="", engine="hython", relink_dirs=["/tex"])
        hits = [w for w in preflight.run_preflight_checks(job, m)
                if w.category == "missing_asset"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].level, "warning")
        self.assertIn("relink will be attempted", hits[0].message)

    def test_without_a_relink_missing_assets_stay_errors(self):
        m = sample_manifest()
        m.missing_assets = [MissingAsset(attr_path="/mat/tex.inputs:file",
                                         asset_path="/tex/wood.exr")]
        job = RenderJob(usd_file="", engine="hython")
        hits = [w for w in preflight.run_preflight_checks(job, m)
                if w.category == "missing_asset"]
        self.assertEqual(hits[0].level, "error")

    def test_preflight_flags_live_volumes_for_husk(self):
        # Live volumes bake ~GB/frame on a husk USD export — warn about it.
        m = sample_manifest()
        m.live_volumes = [LiveVolume(prim_path="/stage/pyro/volume",
                                     field_count=2, field_names=["density", "vel"])]
        warnings = preflight.run_preflight_checks(
            RenderJob(usd_file="/s.usd", engine="husk"), m)
        hits = [w for w in warnings if w.category == "volume_bake"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].level, "warning")
        self.assertIn("density", hits[0].message)

    def test_preflight_live_volumes_ignored_for_hython(self):
        # The hython engine never exports, so there is nothing to bake.
        m = sample_manifest()
        m.live_volumes = [LiveVolume(prim_path="/stage/pyro/volume",
                                     field_count=1, field_names=["density"])]
        warnings = preflight.run_preflight_checks(
            RenderJob(usd_file="", engine="hython"), m)
        self.assertFalse(any(w.category == "volume_bake" for w in warnings))


class TestDefaultEngine(unittest.TestCase):
    """hython is the app default: it renders the ROP with no USD export."""

    def test_default_is_hython(self):
        self.assertEqual(DEFAULT_ENGINE, "hython")
        self.assertEqual(RenderJob(usd_file="/s.usd").engine, "hython")

    def test_jobs_for_rop_defaults_to_hython(self):
        m = sample_manifest()
        job = jobs_for_rop(m, m.rops[0], "/tmp/shot.usd")[0]
        self.assertEqual(job.engine, "hython")
        # The hython command renders the .hip, so the hip path must come through.
        self.assertEqual(job.hip_file, m.hip_path)
        self.assertEqual(job.rop_path, m.rops[0].node_path)

    def test_default_job_builds_a_direct_render_command(self):
        m = sample_manifest()
        cmd = build_command(jobs_for_rop(m, m.rops[0], "/tmp/shot.usd")[0])
        self.assertIn("--render-direct", cmd)
        self.assertIn("-m", cmd)
        self.assertIn("hsl.inspector", cmd)
        # A husk-only flag must never appear on a hython command line.
        self.assertNotIn("--make-output-path", cmd)

    def test_husk_still_available_explicitly(self):
        m = sample_manifest()
        cmd = build_command(
            jobs_for_rop(m, m.rops[0], "/tmp/shot.usd", engine="husk")[0])
        self.assertNotIn("--render-direct", cmd)
        self.assertEqual(cmd[-1], "/tmp/shot.usd")


class TestDirectOverlayPaths(unittest.TestCase):
    def _replace_inspector_attr(self, name, value):
        original = getattr(inspector, name)
        setattr(inspector, name, value)
        self.addCleanup(setattr, inspector, name, original)

    def _set_fake_pid(self, value):
        original = inspector.os.getpid
        inspector.os.getpid = lambda: value
        self.addCleanup(setattr, inspector.os, "getpid", original)

    def test_parallel_hython_processes_get_distinct_overlay_paths(self):
        self._set_fake_pid(101)
        first = inspector._direct_overlay_path("relink")
        inspector.os.getpid = lambda: 202
        second = inspector._direct_overlay_path("relink")

        self.assertNotEqual(first, second)
        self.assertEqual(os.path.basename(first), "relink_direct_101.usda")
        self.assertEqual(os.path.basename(second), "relink_direct_202.usda")

    def test_relink_call_site_uses_process_scoped_path(self):
        captured = {}
        self._set_fake_pid(303)
        self._replace_inspector_attr("stage_for", lambda *args: (object(), None))

        def fake_author(stage, search_dirs, out_path, warnings):
            captured["path"] = out_path
            return {"out": out_path, "relinked": ["asset"], "still_missing": []}

        self._replace_inspector_attr("author_relink_overlay", fake_author)
        self._replace_inspector_attr("_sublayer_into_network",
                                     lambda *args: True)

        count = inspector._insert_relink_layer(object(), ["C:/textures"], [])

        self.assertEqual(count, 1)
        self.assertEqual(os.path.basename(captured["path"]),
                         "relink_direct_303.usda")

    def test_settings_call_site_uses_process_scoped_path(self):
        captured = {}
        self._set_fake_pid(404)

        class FakeHipFile:
            @staticmethod
            def load(*args, **kwargs):
                pass

        class FakeHou:
            Error = RuntimeError
            hipFile = FakeHipFile()

        class FakeRop:
            def parm(self, name):
                return None

            def path(self):
                return "/stage/render"

            def render(self, *args, **kwargs):
                pass

        def fake_author(stage, overrides, out_path, warnings=None):
            captured["path"] = out_path
            return {"out": out_path, "applied": [], "skipped": []}

        self._replace_inspector_attr("hou", FakeHou())
        self._replace_inspector_attr("find_render_rops", lambda: [FakeRop()])
        self._replace_inspector_attr("stage_for", lambda *args: (object(), None))
        self._replace_inspector_attr("author_settings_overlay", fake_author)
        self._replace_inspector_attr("_sublayer_into_network",
                                     lambda *args: True)

        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            result = inspector.render_direct(
                "shot.hip", frame_start=1,
                settings_overrides={"karma:global:samplesperpixel": 64},
            )

        self.assertEqual(result, 0)
        self.assertEqual(os.path.basename(captured["path"]),
                         "settings_direct_404.usda")


class TestCliEngineGuards(unittest.TestCase):
    """--aovs / --relink-from edit the exported USD, so hython must reject them
    rather than silently ignore them."""

    def test_render_defaults_to_hython(self):
        self.assertEqual(build_parser().parse_args(["render", "s.hip"]).engine,
                         "hython")

    def _render(self, argv):
        """cmd_render with its explanatory stderr swallowed, so test output stays clean."""
        args = build_parser().parse_args(argv)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = cmd_render(args)
        return code, err.getvalue()

    def test_aovs_with_hython_is_rejected(self):
        # Rejected on argv alone -- no Houdini launch, so this is safe to assert.
        code, message = self._render(["render", "s.hip", "--aovs", "beauty"])
        self.assertEqual(code, 4)
        self.assertIn("--engine husk", message)

    def test_relink_is_accepted_on_hython(self):
        # hython sublayers the repaths into the LOP network, so this is NOT
        # husk-only: the guard must not fire (it fails later on the missing hip).
        args = build_parser().parse_args(["render", "s.hip", "--relink-from", "/tex"])
        with self.assertRaises(bridge.InspectError):
            cmd_render(args)

    def test_relink_dirs_reach_the_hython_command(self):
        m = sample_manifest()
        job = jobs_for_rop(m, m.rops[0], "", engine="hython",
                           relink_dirs=["/tex/a", "/tex/b"])[0]
        cmd = build_command(job)
        self.assertEqual(cmd[cmd.index("--search") + 1], "/tex/a")
        self.assertEqual(cmd.count("--search"), 2)

    def test_husk_command_never_gets_search_dirs(self):
        m = sample_manifest()
        job = jobs_for_rop(m, m.rops[0], "/tmp/s.usd", engine="husk",
                           relink_dirs=["/tex/a"])[0]
        self.assertNotIn("--search", build_command(job))

    def test_aovs_allowed_with_explicit_husk(self):
        args = build_parser().parse_args(
            ["render", "s.hip", "--engine", "husk", "--aovs", "beauty"])
        # The guard must NOT fire; it proceeds and fails later on the missing hip.
        with self.assertRaises(bridge.InspectError):
            cmd_render(args)


class TestCliPreflight(unittest.TestCase):
    """Preflight used to run only in the GUI; cmd_render must enforce it too."""

    def setUp(self):
        self.manifest = sample_manifest()
        self.manifest.rops[0].usd_path = "/tmp/shot.usd"
        self._real_inspect = bridge.inspect_hip
        # Stand in for the hython subprocess so this needs no Houdini.
        bridge.inspect_hip = lambda *a, **k: self.manifest
        self.addCleanup(self._restore)

    def _restore(self):
        bridge.inspect_hip = self._real_inspect

    def _render(self, extra):
        args = build_parser().parse_args(["render", "s.hip"] + extra)
        with contextlib.redirect_stderr(io.StringIO()) as err, \
                contextlib.redirect_stdout(io.StringIO()):
            code = cmd_render(args)
        return code, err.getvalue()

    def test_missing_texture_blocks_the_render(self):
        self.manifest.missing_assets = [
            MissingAsset(attr_path="/mat/tex.inputs:file", asset_path="/tex/wood.exr")]
        code, message = self._render([])
        self.assertEqual(code, 5)
        self.assertIn("preflight error", message)
        self.assertIn("wood.exr", message)

    def test_skip_preflight_overrides_the_block(self):
        self.manifest.missing_assets = [
            MissingAsset(attr_path="/mat/tex.inputs:file", asset_path="/tex/wood.exr")]
        # Not 5: it proceeds past preflight (and fails later trying to render).
        code, _ = self._render(["--skip-preflight", "--dry-run"])
        self.assertNotEqual(code, 5)

    def test_dry_run_reports_but_does_not_block(self):
        self.manifest.missing_assets = [
            MissingAsset(attr_path="/mat/tex.inputs:file", asset_path="/tex/wood.exr")]
        code, message = self._render(["--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("wood.exr", message)

    def test_clean_scene_passes_preflight(self):
        code, message = self._render(["--dry-run"])
        self.assertEqual(code, 0)
        self.assertNotIn("preflight error", message)


class TestExportNarrowing(unittest.TestCase):
    """The export used to cover the ROP's whole authored range regardless of
    --frames, which is most of the cost on a heavy scene."""

    def setUp(self):
        self.captured = {}
        self.manifest = sample_manifest()
        self.manifest.rops[0].usd_path = "/tmp/shot.usd"
        self._real = bridge.inspect_hip

        def fake(*args, **kwargs):
            self.captured.update(kwargs)
            return self.manifest

        bridge.inspect_hip = fake
        self.addCleanup(lambda: setattr(bridge, "inspect_hip", self._real))

    def _run(self, extra):
        args = build_parser().parse_args(["render", "s.hip", "--dry-run"] + extra)
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            cmd_render(args)

    def test_husk_export_is_narrowed_to_requested_frames(self):
        self._run(["--engine", "husk", "--frames", "1050-1060"])
        self.assertTrue(self.captured["export_usd"])
        self.assertEqual(self.captured["export_frames"], (1050, 1060, 1))

    def test_hython_exports_nothing_at_all(self):
        self._run(["--frames", "1050-1060"])
        self.assertFalse(self.captured["export_usd"])
        self.assertIsNone(self.captured["export_frames"])


class TestSettingOverrides(unittest.TestCase):
    """Karma knobs are USD attributes — husk has no flag for them."""

    def test_parses_key_value(self):
        self.assertEqual(
            husk_mod.parse_setting_args(["karma:global:samplesperpixel=64"]),
            {"karma:global:samplesperpixel": "64"})

    def test_value_may_contain_equals(self):
        self.assertEqual(husk_mod.parse_setting_args(["k=a=b"]), {"k": "a=b"})

    def test_missing_equals_is_rejected(self):
        with self.assertRaises(ValueError):
            husk_mod.parse_setting_args(["karma:global:samplesperpixel"])

    def test_empty_key_is_rejected(self):
        with self.assertRaises(ValueError):
            husk_mod.parse_setting_args(["=64"])

    def test_reaches_the_hython_command(self):
        m = sample_manifest()
        job = jobs_for_rop(m, m.rops[0], "", engine="hython",
                           settings_overrides={"karma:global:samplesperpixel": "64"})[0]
        cmd = build_command(job)
        self.assertEqual(cmd[cmd.index("--set") + 1],
                         "karma:global:samplesperpixel=64")

    def test_husk_command_never_gets_set_flags(self):
        # husk would reject an unknown option; the overlay carries these instead.
        m = sample_manifest()
        job = jobs_for_rop(m, m.rops[0], "/tmp/s.usd", engine="husk",
                           settings_overrides={"karma:global:samplesperpixel": "64"})[0]
        self.assertNotIn("--set", build_command(job))

    def test_set_does_not_clobber_the_settings_prim_argument(self):
        args = build_parser().parse_args(
            ["render", "s.hip", "--settings", "/Render/rs", "--set", "k=1"])
        self.assertEqual(args.settings, "/Render/rs")
        self.assertEqual(args.setting_overrides, ["k=1"])

    def test_malformed_set_is_rejected_before_loading_the_scene(self):
        args = build_parser().parse_args(["render", "s.hip", "--set", "nonsense"])
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = cmd_render(args)
        self.assertEqual(code, 4)
        self.assertIn("KEY=VALUE", err.getvalue())


class TestPlannedOutputs(unittest.TestCase):
    """The filename preview must match what the overlay actually authors."""

    def test_file_mode_first_product_exact_rest_alongside(self):
        pairs = husk_mod.planned_product_paths(
            [("/R/P/beauty", "/old/shot_beauty.$F4.exr"),
             ("/R/P/crypto", "/old/shot_crypto.$F4.exr")],
            "/renders/v2/hero.exr")
        self.assertEqual(pairs[0][1], "/renders/v2/hero.exr")
        self.assertEqual(pairs[1][1], "/renders/v2/shot_crypto.$F4.exr")

    def test_directory_mode_keeps_every_filename(self):
        pairs = husk_mod.planned_product_paths(
            [("/R/P/beauty", "/old/shot_beauty.$F4.exr"),
             ("/R/P/crypto", "/old/shot_crypto.$F4.exr")],
            "/renders/v3/")
        self.assertEqual([p for _, p in pairs],
                         ["/renders/v3/shot_beauty.$F4.exr",
                          "/renders/v3/shot_crypto.$F4.exr"])

    def test_empty_product_name_falls_back_to_the_prim(self):
        # Exactly the real shot: products with no authored productName.
        pairs = husk_mod.planned_product_paths(
            [("/R/P/beauty", ""), ("/R/P/depth", "")], "/renders/v2/hero.exr")
        self.assertEqual(pairs[1][1], "/renders/v2/depth.exr")

    def test_preview_resolves_frames(self):
        m = sample_manifest()
        entries = husk_mod.planned_outputs(m, m.rops[0], frames=[1001, 1002])
        beauty = next(e for e in entries if e["product"].endswith("beauty"))
        self.assertEqual(beauty["files"], ["/renders/shot_beauty.1001.exr",
                                           "/renders/shot_beauty.1002.exr"])
        self.assertFalse(beauty["unresolved"])

    def test_preview_follows_the_output_override(self):
        m = sample_manifest()
        entries = husk_mod.planned_outputs(m, m.rops[0], output="/out/v9/",
                                           frames=[1001])
        self.assertEqual([e["files"][0] for e in entries],
                         ["/out/v9/shot_beauty.1001.exr",
                          "/out/v9/shot_crypto.1001.exr"])

    def test_unexpandable_token_is_reported_not_invented(self):
        m = sample_manifest()
        m.products[0].product_name = "/renders/shot.$FF.exr"
        entries = husk_mod.planned_outputs(m, m.rops[0], frames=[1001])
        self.assertTrue(entries[0]["unresolved"])
        self.assertEqual(entries[0]["files"], [])

    def test_untokenised_name_is_one_file_not_one_per_frame(self):
        m = sample_manifest()
        for product in m.products:
            product.product_name = "/renders/single.exr"
        entries = husk_mod.planned_outputs(m, m.rops[0], frames=[1, 2, 3])
        self.assertEqual(entries[0]["files"], ["/renders/single.exr"])

    def test_folder_with_no_products_names_no_files(self):
        # Nothing declares a filename, so inventing one would be a lie.
        m = sample_manifest()
        m.settings[0].products = []
        entries = husk_mod.planned_outputs(m, m.rops[0], output="/out/v3/",
                                           frames=[1, 2, 3])
        self.assertEqual(entries[0]["files"], [])
        self.assertEqual(entries[0]["template"], "/out/v3/")
        self.assertFalse(entries[0]["unresolved"])

    def test_no_declared_output_is_an_empty_plan(self):
        m = sample_manifest()
        for product in m.products:
            product.product_name = ""
        self.assertEqual(husk_mod.planned_outputs(m, m.rops[0], frames=[1001]), [])


class TestMultiProductOutput(unittest.TestCase):
    """husk -o moves only the first product; a multi-product shot needs the
    USD overlay, and must not also pass -o (that would double-apply)."""

    def setUp(self):
        self.manifest = sample_manifest()       # two products
        self.manifest.rops[0].usd_path = "/tmp/shot.usd"
        self._real_inspect = bridge.inspect_hip
        bridge.inspect_hip = lambda *a, **k: self.manifest
        self.addCleanup(lambda: setattr(bridge, "inspect_hip", self._real_inspect))

    def _dry_run(self, extra):
        args = build_parser().parse_args(["render", "s.hip", "--dry-run"] + extra)
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()):
            code = cmd_render(args)
        return code, out.getvalue()

    def test_multi_product_output_announces_the_overlay(self):
        code, out = self._dry_run(["--engine", "husk", "--output", "/renders/out.exr"])
        self.assertEqual(code, 0)
        self.assertIn("2 products will be redirected", out)

    def test_single_product_uses_husk_flag_directly(self):
        settings = self.manifest.settings[0]
        settings.products = settings.products[:1]        # one product only
        code, out = self._dry_run(["--engine", "husk", "--output", "/renders/out.exr"])
        self.assertEqual(code, 0)
        self.assertNotIn("redirected", out)
        self.assertIn("--output", out)                   # husk's own flag


class TestManifestCache(unittest.TestCase):
    """T8: the CLI can reuse a scene read, but only of an unchanged .hip."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.hip = os.path.join(self.dir, "shot.hip")
        with open(self.hip, "w") as fh:
            fh.write("not really a hip")

    def test_cache_key_is_stable(self):
        # It used to use hash(), which is randomised per process -- so the
        # cache filename changed every run and could never hit.
        self.assertEqual(bridge.cache_path_for(self.hip),
                         bridge.cache_path_for(self.hip))

    def test_cache_key_differs_per_hip(self):
        other = os.path.join(self.dir, "other.hip")
        self.assertNotEqual(bridge.cache_path_for(self.hip),
                            bridge.cache_path_for(other))

    def test_round_trip_through_the_cache(self):
        manifest = sample_manifest()
        manifest.hip_path = self.hip
        bridge.save_cached(manifest)
        self.addCleanup(lambda: os.path.exists(bridge.cache_path_for(self.hip))
                        and os.remove(bridge.cache_path_for(self.hip)))
        loaded = bridge.load_cached(self.hip)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.rops[0].node_path, manifest.rops[0].node_path)

    def test_a_newer_hip_invalidates_the_cache(self):
        manifest = sample_manifest()
        manifest.hip_path = self.hip
        cache = bridge.save_cached(manifest)
        self.addCleanup(lambda: os.path.exists(cache) and os.remove(cache))
        # Touch the hip to be newer than its cached read.
        future = os.path.getmtime(cache) + 60
        os.utime(self.hip, (future, future))
        self.assertIsNone(bridge.load_cached(self.hip))

    def test_missing_cache_is_not_an_error(self):
        self.assertIsNone(bridge.load_cached(os.path.join(self.dir, "never.hip")))


class TestHythonSelection(unittest.TestCase):
    INSTALLS = [("Houdini 22.0.368", "/opt/hfs22/bin/hython"),
                ("Houdini 21.0.729", "/opt/hfs21/bin/hython")]

    def test_index_picks_that_install(self):
        self.assertEqual(_resolve_hython_choice(self.INSTALLS, "2"),
                         "/opt/hfs21/bin/hython")

    def test_index_out_of_range_is_refused(self):
        self.assertEqual(_resolve_hython_choice(self.INSTALLS, "9"), "")

    def test_path_that_does_not_exist_is_refused(self):
        self.assertEqual(_resolve_hython_choice(self.INSTALLS, "/no/such/hython"), "")

    def test_real_path_is_accepted(self):
        self.assertEqual(_resolve_hython_choice(self.INSTALLS, __file__), __file__)

    def test_empty_choice_is_refused(self):
        self.assertEqual(_resolve_hython_choice(self.INSTALLS, ""), "")


class TestPresets(unittest.TestCase):
    def test_default_presets(self):
        all_p = presets.get_default_presets()
        self.assertIn("🚀 Fast Preview (Karma XPU)", all_p)
        self.assertIn("🎨 Beauty Final (Production)", all_p)

    def test_apply_preset(self):
        job = RenderJob(usd_file="/jobs/shot.usd")
        preset = presets.DEFAULT_PRESETS["🚀 Fast Preview (Karma XPU)"]
        updated = presets.apply_preset(job, preset)
        self.assertEqual(updated.renderer, "BRAY_HdKarmaXPU")
        self.assertEqual(updated.resolution, (960, 540))


class TestFarmDistribution(unittest.TestCase):
    """T7: the exporter must distribute frames. It used to emit job[0]'s
    command verbatim, so every farm task re-rendered that one chunk."""

    def jobs(self):
        m = sample_manifest()                      # 1001-1100
        return jobs_for_rop(m, m.rops[0], "/tmp/shot.usd",
                            engine="husk", husk_exe="husk", chunk_size=25)

    def test_frame_expression_collapses_runs(self):
        jobs = self.jobs()
        self.assertEqual(len(jobs), 4)
        self.assertEqual(farm.frame_expression(jobs), "1001-1100")

    def test_frame_expression_handles_gaps_and_steps(self):
        job = RenderJob(usd_file="/s.usd", engine="husk", chunk=FrameChunk(1, 3, 5))
        # frames 1, 6, 11 -- no consecutive run to collapse
        self.assertEqual(farm.frame_expression([job]), "1,6,11")

    def test_every_frame_is_covered_exactly_once(self):
        frames = farm.all_frames(self.jobs())
        self.assertEqual(frames, list(range(1001, 1101)))
        self.assertEqual(len(frames), len(set(frames)))

    def test_task_command_is_tokenised_not_hardcoded(self):
        argv = farm.task_command(self.jobs()[0], farm.DEADLINE_START_TOKEN)
        self.assertIn(farm.DEADLINE_START_TOKEN, argv)
        self.assertEqual(argv[argv.index("--frame") + 1], farm.DEADLINE_START_TOKEN)
        self.assertEqual(argv[argv.index("--frame-count") + 1], "1")
        # The literal start frame must be gone, or every task renders chunk 0.
        self.assertNotIn("1001", argv)

    def test_hython_jobs_tokenise_their_own_frame_flag(self):
        m = sample_manifest()
        job = jobs_for_rop(m, m.rops[0], "", engine="hython", chunk_size=25)[0]
        argv = farm.task_command(job, farm.DEADLINE_START_TOKEN)
        self.assertEqual(argv[argv.index("--frame-start") + 1],
                         farm.DEADLINE_START_TOKEN)

    def test_deadline_files_carry_token_and_full_range(self):
        temp_dir = tempfile.mkdtemp()
        try:
            job_path, plugin_path = farm.export_deadline_job(self.jobs(), temp_dir)
            with open(job_path, encoding="utf-8") as fh:
                job_info = fh.read()
            with open(plugin_path, encoding="utf-8") as fh:
                plugin_info = fh.read()
            self.assertIn("Frames=1001-1100", job_info)
            self.assertIn("ChunkSize=1", job_info)
            self.assertIn(farm.DEADLINE_START_TOKEN, plugin_info)
            self.assertNotIn("--frame 1001", plugin_info)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_tractor_emits_one_task_per_chunk_with_real_frames(self):
        temp_dir = tempfile.mkdtemp()
        try:
            out = farm.export_tractor_job(
                self.jobs(), os.path.join(temp_dir, "job.alf"))
            with open(out, encoding="utf-8") as fh:
                content = fh.read()
            self.assertEqual(content.count("RemoteCmd"), 4)
            for start in ("1001", "1026", "1051", "1076"):
                self.assertIn(start, content)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


class TestFarm(unittest.TestCase):
    def test_export_deadline_job(self):
        job = RenderJob(usd_file="/jobs/shot.usd", engine="husk", husk_exe="husk")
        temp_dir = tempfile.mkdtemp()
        try:
            j_path, p_path = farm.export_deadline_job([job], temp_dir)
            self.assertTrue(os.path.isfile(j_path))
            self.assertTrue(os.path.isfile(p_path))
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_export_tractor_job(self):
        job = RenderJob(usd_file="/jobs/shot.usd", engine="husk", husk_exe="husk")
        temp_dir = tempfile.mkdtemp()
        try:
            out_file = os.path.join(temp_dir, "tractor_job.alf")
            res = farm.export_tractor_job([job], out_file)
            self.assertTrue(os.path.isfile(res))
            with open(res, "r", encoding="utf-8") as fh:
                content = fh.read()
            self.assertIn("Job -title", content)
            self.assertIn("RemoteCmd", content)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


class TestAppIcon(unittest.TestCase):
    """The taskbar icon is an asset, not code, so the thing worth testing is
    that it ships and that Windows can actually parse it. ``ui.py`` itself
    stays out of here -- it needs Qt, and this suite must not."""

    def _entries(self):
        """Parse the ICO directory: [(width, height, nbytes, offset), ...]."""
        with open(resources.icon_path(), "rb") as fh:
            blob = fh.read()
        reserved, kind, count = struct.unpack("<HHH", blob[:6])
        self.assertEqual(reserved, 0)
        self.assertEqual(kind, 1, "type 1 is an icon; 2 would be a cursor")
        entries = []
        for i in range(count):
            head = blob[6 + i * 16:22 + i * 16]
            width, height, _colors, _res, _planes, _bpp, nbytes, offset = \
                struct.unpack("<BBBBHHII", head)
            # The size byte is one byte wide, so 256 has to be encoded as 0.
            entries.append((width or 256, height or 256, nbytes, offset))
        return blob, entries

    def test_icon_ships_inside_the_package(self):
        path = resources.icon_path()
        self.assertTrue(path, "icon_path() returned empty -- asset missing")
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(os.path.basename(path), "hsl.ico")
        # Inside hsl/ so it survives an install, not next to the repo root.
        self.assertEqual(os.path.basename(os.path.dirname(path)), "assets")

    def test_icon_carries_the_sizes_windows_asks_for(self):
        _blob, entries = self._entries()
        sizes = {width for width, _h, _n, _o in entries}
        # 16 = taskbar and title bar, 32 = alt-tab, 48 = Explorer,
        # 256 = the large tile. A missing size gets downscaled by the shell.
        for expected in (16, 32, 48, 256):
            self.assertIn(expected, sizes)
        for width, height, _n, _o in entries:
            self.assertEqual(width, height, "icons must be square")

    def test_every_entry_points_inside_the_file(self):
        """Catches a mis-packed directory, which renders as a blank icon
        rather than as any kind of error."""
        blob, entries = self._entries()
        for width, _h, nbytes, offset in entries:
            self.assertGreater(nbytes, 0, f"{width}px entry is empty")
            self.assertLessEqual(offset + nbytes, len(blob),
                                 f"{width}px entry runs past the end of the file")

    def test_missing_asset_reports_empty_rather_than_raising(self):
        """A stripped install should lose its icon, not fail to launch."""
        temp_dir = tempfile.mkdtemp()
        original = resources.ASSET_DIR
        resources.ASSET_DIR = temp_dir
        try:
            self.assertEqual(resources.icon_path(), "")
        finally:
            resources.ASSET_DIR = original
            shutil.rmtree(temp_dir, ignore_errors=True)


class TestOutputTask(unittest.TestCase):
    """The generalised scheduling unit: any cookable node, not just Solaris."""

    def test_frame_count_matches_the_range(self):
        task = OutputTask(node_path="/out/cache", use_frame_range=True,
                          frame_start=1, frame_end=100, frame_inc=2)
        self.assertEqual(task.frame_count, 50)

    def test_single_frame_task_counts_one(self):
        self.assertEqual(OutputTask(node_path="/out/x").frame_count, 1)

    def test_a_simulation_cannot_declare_itself_parallelisable(self):
        """Frame N of a sim depends on N-1. A sim claiming otherwise is a bug,
        so the manifest refuses to represent it rather than trusting it."""
        task = OutputTask(node_path="/out/dop", kind=TASK_SIM, sequential=False)
        self.assertTrue(task.sequential)

    def test_other_kinds_keep_the_flag_they_were_given(self):
        self.assertFalse(OutputTask(node_path="/out/c", kind=TASK_CACHE).sequential)
        self.assertTrue(OutputTask(node_path="/out/c", kind=TASK_CACHE,
                                   sequential=True).sequential)
        self.assertEqual(OutputTask(node_path="/out/x").kind, TASK_UNKNOWN)

    def test_tasks_survive_a_json_round_trip(self):
        manifest = sample_manifest()
        manifest.tasks = [
            OutputTask(node_path="/out/cache", kind=TASK_CACHE,
                       use_frame_range=True, frame_start=1, frame_end=10,
                       outputs=["/jobs/cache/geo.$F4.bgeo.sc"]),
            OutputTask(node_path="/out/dop", kind=TASK_SIM,
                       depends_on=["/out/cache"]),
        ]
        back = SceneManifest.from_json(manifest.to_json())

        self.assertEqual(len(back.tasks), 2)
        self.assertIsInstance(back.tasks[0], OutputTask)
        self.assertEqual(back.tasks[0].outputs, ["/jobs/cache/geo.$F4.bgeo.sc"])
        self.assertEqual(back.tasks[1].depends_on, ["/out/cache"])
        self.assertTrue(back.tasks[1].sequential)

    def test_a_manifest_written_before_tasks_existed_still_loads(self):
        """Schema 1 has no 'tasks' key. Old cached manifests live in %TEMP%
        and must not become a crash on upgrade."""
        old = json.dumps({
            "schema_version": 1,
            "hip_path": "/jobs/shot.hip",
            "rops": [{"node_path": "/stage/r1", "node_type": "usdrender_rop"}],
        })
        manifest = SceneManifest.from_json(old)
        self.assertEqual(manifest.tasks, [])
        self.assertEqual(len(manifest.rops), 1)

    def test_lookups(self):
        manifest = SceneManifest(tasks=[
            OutputTask(node_path="/out/a", kind=TASK_CACHE),
            OutputTask(node_path="/out/b", kind=TASK_RENDER),
        ])
        self.assertEqual(manifest.task("/out/a").kind, TASK_CACHE)
        self.assertIsNone(manifest.task("/out/nope"))
        self.assertEqual([t.node_path for t in manifest.tasks_of_kind(TASK_RENDER)],
                         ["/out/b"])


class TestSequentialChunking(unittest.TestCase):
    """Splitting a simulation across processes corrupts it silently, so the
    chunker must refuse to do it."""

    def _task(self, **kw):
        base = dict(node_path="/out/task", use_frame_range=True,
                    frame_start=1, frame_end=100, frame_inc=1)
        base.update(kw)
        return OutputTask(**base)

    def test_independent_frames_chunk_normally(self):
        chunks = chunks_for_task(self._task(kind=TASK_CACHE), chunk_size=10)
        self.assertEqual(len(chunks), 10)
        self.assertEqual(chunks[0].start, 1)
        self.assertEqual(chunks[0].count, 10)

    def test_a_simulation_is_never_split_however_it_is_asked(self):
        chunks = chunks_for_task(self._task(kind=TASK_SIM), chunk_size=10)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].start, 1)
        self.assertEqual(chunks[0].count, 100)

    def test_sequential_flag_alone_is_enough(self):
        """A cache reading from a solver carries state between frames too."""
        task = self._task(kind=TASK_CACHE, sequential=True)
        self.assertEqual(len(chunks_for_task(task, chunk_size=5)), 1)

    def test_single_frame_task_gives_one_chunk(self):
        chunks = chunks_for_task(self._task(use_frame_range=False), chunk_size=10)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].count, 1)

    def test_jobs_carry_identity_and_dependencies_onto_every_chunk(self):
        manifest = SceneManifest(hip_path="/jobs/shot.hip")
        task = self._task(kind=TASK_CACHE, depends_on=["/out/upstream"],
                          outputs=["/jobs/geo.$F4.bgeo.sc"])
        jobs = jobs_for_task(manifest, task, chunk_size=25)

        self.assertEqual(len(jobs), 4)
        for job in jobs:
            self.assertEqual(job.task_id, "/out/task")
            self.assertEqual(job.depends_on, ["/out/upstream"])
            self.assertEqual(job.expected_outputs, ["/jobs/geo.$F4.bgeo.sc"])
            self.assertEqual(job.engine, "hython")
            self.assertEqual(job.renderer, "")   # meaningless for a cache

    def test_husk_is_refused_for_work_it_cannot_do(self):
        """husk consumes USD; it cannot cook a SOP or advance a solver."""
        manifest = SceneManifest(hip_path="/jobs/shot.hip")
        with self.assertRaises(ValueError) as ctx:
            jobs_for_task(manifest, self._task(kind=TASK_CACHE), engine="husk")
        self.assertIn("husk", str(ctx.exception))

    def test_husk_is_still_allowed_for_a_solaris_render_rop(self):
        """husk drives USD, so it is fine for the one kind of render that has
        an exported stage behind it."""
        manifest = SceneManifest(hip_path="/jobs/shot.hip")
        manifest.rops = [RenderRop(node_path="/stage/usdrender_rop1",
                                   node_type="usdrender_rop",
                                   frame_start=1, frame_end=100,
                                   use_frame_range=True)]
        task = self._task(kind=TASK_RENDER, node_path="/stage/usdrender_rop1")
        jobs = jobs_for_task(manifest, task, engine="husk", chunk_size=50)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0].engine, "husk")
        self.assertFalse(jobs[0].cook)

    def test_husk_is_refused_for_a_render_it_cannot_drive(self):
        manifest = SceneManifest(hip_path="/jobs/shot.hip")
        with self.assertRaises(ValueError) as ctx:
            jobs_for_task(manifest, self._task(kind=TASK_RENDER,
                                               node_path="/out/mantra1"),
                          engine="husk")
        self.assertIn("husk", str(ctx.exception))


class TestFarmDependencies(unittest.TestCase):
    """A farm runs tasks on different machines with nothing to serialise them,
    so a dropped dependency there is worse than locally."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.manifest = SceneManifest(hip_path="/jobs/shot.hip")

    def _read(self, path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def _jobs(self):
        cache = OutputTask(node_path="/out/cache", kind=TASK_CACHE,
                           use_frame_range=True, frame_start=1, frame_end=4)
        sim = OutputTask(node_path="/out/sim", kind=TASK_SIM,
                         use_frame_range=True, frame_start=1, frame_end=4,
                         depends_on=["/out/cache"])
        return (jobs_for_task(self.manifest, cache, chunk_size=2)
                + jobs_for_task(self.manifest, sim))

    def test_tractor_orders_dependent_work(self):
        path = farm.export_tractor_job(self._jobs(),
                                       os.path.join(self.dir, "job.alf"))
        text = self._read(path)
        self.assertIn("-id {/out/cache}", text)
        self.assertIn("Instance {/out/cache}", text)
        # The dependent group must run its prerequisites before its own work.
        self.assertIn("-serialsubtasks 1", text)
        waits = text.index("Instance {/out/cache}")
        work = text.index("--rop /out/sim") if "--rop /out/sim" in text \
            else text.index("/out/sim")
        self.assertLess(waits, work)

    def test_a_shared_dependency_is_referenced_not_duplicated(self):
        """Two dependants must not each run the cache."""
        cache = OutputTask(node_path="/out/cache", kind=TASK_CACHE)
        a = OutputTask(node_path="/out/a", kind=TASK_CACHE,
                       depends_on=["/out/cache"])
        b = OutputTask(node_path="/out/b", kind=TASK_CACHE,
                       depends_on=["/out/cache"])
        jobs = sum((jobs_for_task(self.manifest, t) for t in (cache, a, b)), [])
        path = farm.export_tractor_job(jobs, os.path.join(self.dir, "d.alf"))
        text = self._read(path)
        self.assertEqual(text.count("-id {/out/cache}"), 1)
        self.assertEqual(text.count("Instance {/out/cache}"), 2)
        self.assertEqual(text.count("--rop /out/cache"), 1)

    def test_independent_work_keeps_the_flat_shape(self):
        """The Solaris path has no task ids; its output must not change."""
        jobs = [RenderJob(usd_file="/jobs/shot.usd", engine="husk",
                          husk_exe="husk", chunk=FrameChunk(n, 1, 1))
                for n in (1, 2)]
        path = farm.export_tractor_job(jobs, os.path.join(self.dir, "f.alf"))
        text = self._read(path)
        self.assertNotIn("Instance", text)
        self.assertNotIn("-serialsubtasks", text)
        self.assertEqual(text.count("RemoteCmd"), 2)

    def test_deadline_refuses_work_it_cannot_order(self):
        with self.assertRaises(ValueError) as ctx:
            farm.export_deadline_job(self._jobs(), self.dir)
        message = str(ctx.exception)
        self.assertIn("dependenc", message.lower())
        self.assertIn("Tractor", message)

    def test_deadline_refuses_several_tasks_in_one_job(self):
        cache = OutputTask(node_path="/out/cache", kind=TASK_CACHE)
        other = OutputTask(node_path="/out/other", kind=TASK_CACHE)
        jobs = (jobs_for_task(self.manifest, cache)
                + jobs_for_task(self.manifest, other))
        with self.assertRaises(ValueError):
            farm.export_deadline_job(jobs, self.dir)

    def test_deadline_still_exports_a_plain_render(self):
        jobs = [RenderJob(usd_file="/jobs/shot.usd", engine="husk",
                          husk_exe="husk", chunk=FrameChunk(1, 4, 1))]
        info, plugin = farm.export_deadline_job(jobs, self.dir)
        self.assertTrue(os.path.isfile(info))
        self.assertTrue(os.path.isfile(plugin))


class TestCookCli(unittest.TestCase):
    """`hsl cook` selection and reporting. The queue itself is covered by
    TestQueueDependencies; this is about picking the right work."""

    def _manifest(self):
        return SceneManifest(hip_path="/jobs/shot.hip", tasks=[
            OutputTask(node_path="/out/cache", kind=TASK_CACHE,
                       use_frame_range=True, frame_start=1, frame_end=10),
            OutputTask(node_path="/out/sim", kind=TASK_SIM,
                       use_frame_range=True, frame_start=1, frame_end=10,
                       depends_on=["/out/cache"]),
            OutputTask(node_path="/out/mantra", kind=TASK_RENDER),
        ])

    def _args(self, argv):
        return build_parser().parse_args(argv)

    def test_cook_defaults_leave_rendering_to_the_render_command(self):
        tasks, missing = cli_mod._select_tasks(
            self._manifest(), [], (TASK_CACHE, TASK_SIM))
        self.assertEqual([t.node_path for t in tasks], ["/out/cache", "/out/sim"])
        self.assertEqual(missing, [])

    def test_explicit_task_paths_override_the_kind_filter(self):
        tasks, missing = cli_mod._select_tasks(
            self._manifest(), ["/out/mantra"], (TASK_CACHE,))
        self.assertEqual([t.node_path for t in tasks], ["/out/mantra"])
        self.assertEqual(missing, [])

    def test_an_unknown_task_path_is_reported_not_ignored(self):
        tasks, missing = cli_mod._select_tasks(
            self._manifest(), ["/out/nope"], (TASK_CACHE,))
        self.assertEqual(tasks, [])
        self.assertEqual(missing, ["/out/nope"])

    def test_the_plan_says_when_chunking_was_dropped(self):
        """Silently ignoring --chunk would look like it was honoured."""
        sequential = OutputTask(node_path="/out/sim", kind=TASK_SIM,
                                use_frame_range=True, frame_start=1, frame_end=10)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli_mod._describe_plan([sequential], 5)
        text = out.getvalue()
        self.assertIn("sequential", text)
        self.assertIn("--chunk ignored", text)

    def test_the_plan_shows_dependencies(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli_mod._describe_plan(self._manifest().tasks[1:2], 0)
        self.assertIn("after: /out/cache", out.getvalue())

    def test_one_output_path_cannot_be_shared_by_many_tasks(self):
        manifest = self._manifest()
        original = bridge.inspect_hip
        bridge.inspect_hip = lambda *a, **k: manifest
        try:
            args = self._args(["cook", "s.hip", "--output", "/tmp/one.bgeo.sc"])
            with contextlib.redirect_stderr(io.StringIO()) as err:
                code = cmd_cook(args)
        finally:
            bridge.inspect_hip = original
        self.assertEqual(code, 4)
        self.assertIn("--task", err.getvalue())

    def test_cook_parser_defaults(self):
        args = self._args(["cook", "s.hip"])
        self.assertEqual(args.chunk, 0)
        self.assertEqual(args.parallel, 1)
        self.assertEqual(args.kind, "")
        self.assertEqual(args.task, [])
        self.assertFalse(args.dry_run)
        self.assertFalse(args.no_cache)
        self.assertFalse(args.skip_preflight)


class TestCookCache(unittest.TestCase):
    """`hsl cook` never exports USD, so it can always reuse a cached scene
    read -- the reuse render only gets when it is not exporting."""

    def _manifest(self):
        return SceneManifest(hip_path="/jobs/shot.hip", tasks=[
            OutputTask(node_path="/out/cache", kind=TASK_CACHE,
                       use_frame_range=True, frame_start=1, frame_end=10)])

    def _patch(self, name, value):
        original = getattr(bridge, name)
        setattr(bridge, name, value)
        self.addCleanup(setattr, bridge, name, original)

    def _cook(self, extra):
        args = build_parser().parse_args(["cook", "s.hip", "--dry-run"] + extra)
        with contextlib.redirect_stderr(io.StringIO()) as err, \
                contextlib.redirect_stdout(io.StringIO()):
            code = cmd_cook(args)
        return code, err.getvalue()

    def test_a_cached_read_skips_the_houdini_launch(self):
        self._patch("load_cached", lambda hip: self._manifest())
        self._patch("inspect_hip",
                    lambda *a, **k: self.fail("inspect_hip must not run"))
        code, message = self._cook([])
        self.assertEqual(code, 0)
        self.assertIn("Using the cached scene read", message)

    def test_no_cache_forces_a_fresh_read(self):
        self._patch("load_cached",
                    lambda hip: self.fail("--no-cache must not read the cache"))
        self._patch("inspect_hip", lambda *a, **k: self._manifest())
        self._patch("save_cached", lambda m: "")
        code, message = self._cook(["--no-cache"])
        self.assertEqual(code, 0)
        self.assertNotIn("cached", message)

    def test_a_fresh_read_is_saved_for_next_time(self):
        saved = []
        self._patch("load_cached", lambda hip: None)
        self._patch("inspect_hip", lambda *a, **k: self._manifest())
        self._patch("save_cached", saved.append)
        self._cook([])
        self.assertEqual(len(saved), 1)

    def test_a_cache_that_cannot_be_written_is_not_an_error(self):
        def boom(manifest):
            raise OSError("read-only temp dir")
        self._patch("load_cached", lambda hip: None)
        self._patch("inspect_hip", lambda *a, **k: self._manifest())
        self._patch("save_cached", boom)
        code, _ = self._cook([])
        self.assertEqual(code, 0)


class TestCookPreflight(unittest.TestCase):
    """A cook can fill a disk or write nowhere just as easily as a render;
    until now only render had preflight at the CLI."""

    def test_invalid_frame_range_is_an_error_named_after_its_task(self):
        job = RenderJob(chunk=FrameChunk(1, 0, 1), task_id="/out/cache")
        hits = preflight.run_cook_preflight_checks([job])
        self.assertEqual([w.level for w in hits], ["error"])
        self.assertIn("/out/cache", hits[0].message)

    def test_each_task_is_judged_once_not_per_chunk(self):
        jobs = [RenderJob(chunk=FrameChunk(1, 0, 1), task_id="/out/cache"),
                RenderJob(chunk=FrameChunk(6, 0, 1), task_id="/out/cache")]
        hits = preflight.run_cook_preflight_checks(jobs)
        self.assertEqual(len([w for w in hits if w.category == "frame_range"]), 1)

    def test_expected_outputs_stand_in_for_a_missing_override(self):
        missing_dir = os.path.join(tempfile.gettempdir(), "hsl_nope_xyz", "geo")
        job = RenderJob(chunk=FrameChunk(1, 5, 1), task_id="/out/cache",
                        expected_outputs=[os.path.join(missing_dir, "c.$F4.bgeo.sc")])
        hits = [w for w in preflight.run_cook_preflight_checks([job])
                if w.category == "output_path"]
        self.assertTrue(hits)
        self.assertIn("does not exist", hits[0].message)

    def test_an_override_without_a_frame_token_is_flagged(self):
        job = RenderJob(chunk=FrameChunk(1, 5, 1), task_id="/out/cache",
                        output="/tmp/one.bgeo.sc")
        hits = [w for w in preflight.run_cook_preflight_checks([job])
                if "overwrite" in w.message]
        self.assertEqual(len(hits), 1)

    def test_node_declared_outputs_are_not_second_guessed_for_a_frame_token(self):
        # Only an explicit --output override is checked for a missing frame
        # token (docstring: "Node-declared outputs keep their own tokens").
        # A cache/sim's own outputs are not re-validated here, so a task with
        # no --output override must never trip the "overwrite" warning even
        # when its own declared output has no token.
        job = RenderJob(chunk=FrameChunk(1, 5, 1), task_id="/out/cache",
                        expected_outputs=["/tmp/no_token_here.bgeo.sc"])
        hits = [w for w in preflight.run_cook_preflight_checks([job])
                if "overwrite" in w.message]
        self.assertEqual(hits, [])

    def test_output_checks_are_shared_by_tasks_writing_to_the_same_directory(self):
        # Two unrelated tasks pointed at one directory must not double the
        # same directory-level warning -- seen_dirs is keyed by directory,
        # not by task, deliberately (a cook plan often fans out several
        # tasks into one output folder).
        missing_dir = os.path.join(tempfile.gettempdir(), "hsl_shared_xyz", "geo")
        jobs = [
            RenderJob(chunk=FrameChunk(1, 1, 1), task_id="/out/cache_a",
                      output=os.path.join(missing_dir, "a.bgeo.sc")),
            RenderJob(chunk=FrameChunk(1, 1, 1), task_id="/out/cache_b",
                      output=os.path.join(missing_dir, "b.bgeo.sc")),
        ]
        hits = [w for w in preflight.run_cook_preflight_checks(jobs)
                if w.category == "output_path" and "does not exist" in w.message]
        self.assertEqual(len(hits), 1)

    def test_low_disk_space_is_flagged_per_directory(self):
        job = RenderJob(chunk=FrameChunk(1, 1, 1), task_id="/out/cache",
                        output=os.path.join(tempfile.gettempdir(), "c.bgeo.sc"))
        fake_usage = type("Usage", (), {"free": 100 * 1024 ** 2})()  # ~0.1 GB
        original = preflight.shutil.disk_usage
        preflight.shutil.disk_usage = lambda path: fake_usage
        self.addCleanup(setattr, preflight.shutil, "disk_usage", original)

        hits = [w for w in preflight.run_cook_preflight_checks([job])
                if w.category == "disk_space"]
        self.assertEqual(len(hits), 1)
        self.assertIn("GB remaining", hits[0].message)

    def test_missing_assets_warn_but_do_not_block_a_cook(self):
        # The render preflight makes these errors; a cache or sim does not
        # necessarily read the textures a render does.
        m = SceneManifest(missing_assets=[
            MissingAsset(attr_path="/mat/tex.inputs:file",
                         asset_path="/tex/wood.exr")])
        job = RenderJob(chunk=FrameChunk(1, 1, 1), task_id="/out/cache")
        hits = [w for w in preflight.run_cook_preflight_checks([job], m)
                if w.category == "missing_asset"]
        self.assertEqual([w.level for w in hits], ["warning"])
        self.assertIn("wood.exr", hits[0].message)

    def test_scene_warnings_are_carried_through_as_info(self):
        m = SceneManifest(warnings=["could not read /out/odd"])
        hits = preflight.run_cook_preflight_checks(
            [RenderJob(chunk=FrameChunk(1, 1, 1), task_id="/out/c")], m)
        self.assertIn(("info", "could not read /out/odd"),
                      [(w.level, w.message) for w in hits])

    # -- CLI wiring -------------------------------------------------------

    def _cook(self, extra, checks):
        original = cli_mod.preflight.run_cook_preflight_checks
        cli_mod.preflight.run_cook_preflight_checks = lambda jobs, m=None: checks
        self.addCleanup(setattr, cli_mod.preflight,
                        "run_cook_preflight_checks", original)
        original_inspect = bridge.inspect_hip
        bridge.inspect_hip = lambda *a, **k: SceneManifest(
            hip_path="/jobs/shot.hip",
            tasks=[OutputTask(node_path="/out/cache", kind=TASK_CACHE)])
        self.addCleanup(setattr, bridge, "inspect_hip", original_inspect)
        original_save = bridge.save_cached
        bridge.save_cached = lambda m: ""
        self.addCleanup(setattr, bridge, "save_cached", original_save)
        args = build_parser().parse_args(["cook", "s.hip", "--no-cache"] + extra)
        with contextlib.redirect_stderr(io.StringIO()) as err, \
                contextlib.redirect_stdout(io.StringIO()):
            code = cmd_cook(args)
        return code, err.getvalue()

    def test_a_preflight_error_blocks_the_cook(self):
        checks = [preflight.PreflightWarning("error", "output_path", "boom")]
        code, message = self._cook([], checks)
        self.assertEqual(code, 5)
        self.assertIn("preflight error", message)
        self.assertIn("boom", message)

    def test_skip_preflight_overrides_the_block(self):
        checks = [preflight.PreflightWarning("error", "output_path", "boom")]
        code, _ = self._cook(["--skip-preflight", "--dry-run"], checks)
        self.assertEqual(code, 0)

    def test_dry_run_reports_but_does_not_block(self):
        checks = [preflight.PreflightWarning("error", "output_path", "boom")]
        code, message = self._cook(["--dry-run"], checks)
        self.assertEqual(code, 0)
        self.assertIn("boom", message)


class TestInspectedFrame(unittest.TestCase):
    """T5: the manifest records the frame its description was taken at, and
    --frame reaches the inspector argv."""

    def test_inspected_frame_round_trips_through_json(self):
        m = sample_manifest()
        m.inspected_frame = 1012.0
        self.assertEqual(SceneManifest.from_json(m.to_json()).inspected_frame,
                         1012.0)

    def test_an_old_manifest_without_the_field_still_loads(self):
        data = json.loads(sample_manifest().to_json())
        data.pop("inspected_frame", None)
        again = SceneManifest.from_json(json.dumps(data))
        self.assertIsNone(again.inspected_frame)

    def _inspect_capturing_argv(self, **kwargs):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        hip = os.path.join(d, "s.hip")
        with open(hip, "w") as fh:
            fh.write("x")
        captured = {}

        def fake_run(cmd, **run_kwargs):
            captured["cmd"] = list(cmd)
            json_path = cmd[cmd.index("--json") + 1]
            with open(json_path, "w") as fh:
                fh.write(SceneManifest(hip_path=hip,
                                       inspected_frame=kwargs.get("frame")).to_json())

            class Result:
                returncode, stdout, stderr = 0, "", ""
            return Result()

        original_run = bridge.subprocess.run
        original_find = bridge.find_hython
        bridge.subprocess.run = fake_run
        bridge.find_hython = lambda *a, **k: "hython-stand-in"
        try:
            manifest = bridge.inspect_hip(hip, export_usd=False, **kwargs)
        finally:
            bridge.subprocess.run = original_run
            bridge.find_hython = original_find
        return captured["cmd"], manifest

    def test_frame_reaches_the_inspector_argv(self):
        cmd, manifest = self._inspect_capturing_argv(frame=1012.0)
        self.assertIn("--frame", cmd)
        self.assertEqual(cmd[cmd.index("--frame") + 1], "1012.0")
        self.assertEqual(manifest.inspected_frame, 1012.0)

    def test_no_frame_means_no_flag(self):
        cmd, _ = self._inspect_capturing_argv()
        self.assertNotIn("--frame", cmd)

    def test_inspect_parser_takes_a_frame(self):
        args = build_parser().parse_args(["inspect", "s.hip", "--frame", "1012"])
        self.assertEqual(args.frame, 1012.0)
        self.assertIsNone(build_parser().parse_args(["inspect", "s.hip"]).frame)


class TestInspectCliFrame(unittest.TestCase):
    """cmd_inspect itself must forward --frame to bridge.inspect_hip and
    report back what it got -- TestInspectedFrame above only proves the
    bridge and the parser each work in isolation, not that cmd_inspect wires
    the one into the other."""

    def _inspect(self, extra, manifest):
        captured = {}
        original = bridge.inspect_hip

        def fake(*args, **kwargs):
            captured.update(kwargs)
            return manifest

        bridge.inspect_hip = fake
        self.addCleanup(setattr, bridge, "inspect_hip", original)
        args = build_parser().parse_args(["inspect", "s.hip"] + extra)
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()):
            code = cmd_inspect(args)
        return code, out.getvalue(), captured

    def test_frame_argument_reaches_the_inspector_call(self):
        _, _, kwargs = self._inspect(["--frame", "1012"], sample_manifest())
        self.assertEqual(kwargs.get("frame"), 1012.0)

    def test_no_frame_argument_passes_none_through(self):
        _, _, kwargs = self._inspect([], sample_manifest())
        self.assertIsNone(kwargs.get("frame"))

    def test_report_names_the_frame_it_was_described_at(self):
        m = sample_manifest()
        m.inspected_frame = 1012.0
        _, out, _ = self._inspect(["--frame", "1012"], m)
        self.assertIn("described at frame 1012", out)

    def test_report_says_nothing_about_a_frame_when_none_was_used(self):
        m = sample_manifest()
        m.inspected_frame = None
        _, out, _ = self._inspect([], m)
        self.assertNotIn("described at frame", out)


class TestMultiRopRender(unittest.TestCase):
    """`hsl render` can queue several ROPs. The queue starts chunks in
    submission order, so --parallel 1 renders one ROP after another."""

    def setUp(self):
        self.manifest = sample_manifest()
        self.manifest.rops.append(RenderRop(
            node_path="/stage/usdrender_rop2", node_type="usdrender_rop",
            renderer="BRAY_HdKarma", settings_prim="/Render/rendersettings",
            frame_start=1, frame_end=1))
        self._real_inspect = bridge.inspect_hip
        self._real_save = bridge.save_cached
        bridge.inspect_hip = lambda *a, **k: self.manifest
        bridge.save_cached = lambda m: ""
        self.addCleanup(self._restore)

    def _restore(self):
        bridge.inspect_hip = self._real_inspect
        bridge.save_cached = self._real_save

    def _render(self, extra):
        args = build_parser().parse_args(
            ["render", "s.hip", "--no-cache", "--dry-run"] + extra)
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = cmd_render(args)
        return code, out.getvalue(), err.getvalue()

    def test_two_rops_render_in_the_order_asked_for(self):
        code, out, _ = self._render(["--rop", "/stage/usdrender_rop2",
                                     "--rop", "/stage/usdrender_rop1"])
        self.assertEqual(code, 0)
        self.assertIn("# --- /stage/usdrender_rop2 ---", out)
        self.assertLess(out.index("/stage/usdrender_rop2"),
                        out.index("/stage/usdrender_rop1"))

    def test_all_rops_renders_everything(self):
        code, out, _ = self._render(["--all-rops"])
        self.assertEqual(code, 0)
        self.assertIn("/stage/usdrender_rop1", out)
        self.assertIn("/stage/usdrender_rop2", out)

    def test_multiple_rops_reject_a_single_output_path(self):
        # One file path shared by two ROPs means one silently overwrites the
        # other -- same guard the cook command has.
        code, _, err = self._render(["--all-rops", "--output", "/tmp/x.exr"])
        self.assertEqual(code, 4)
        self.assertIn("--output", err)

    def test_unknown_rop_is_reported_with_the_scene_list(self):
        code, _, err = self._render(["--rop", "/stage/nope"])
        self.assertEqual(code, 2)
        self.assertIn("/stage/nope", err)
        self.assertIn("/stage/usdrender_rop1", err)

    def test_a_duplicate_rop_renders_once_and_says_so(self):
        code, out, err = self._render(["--rop", "/stage/usdrender_rop1",
                                       "--rop", "/stage/usdrender_rop1"])
        self.assertEqual(code, 0)
        self.assertIn("duplicate", err)
        self.assertNotIn("# ---", out)     # collapsed back to a single ROP

    def test_no_selection_still_asks_the_user_to_choose(self):
        # Two ROPs and no --rop stays an error: rendering everything is the
        # explicit --all-rops opt-in, not a silent default.
        code, _, err = self._render([])
        self.assertEqual(code, 2)
        self.assertIn("--rop", err)

    def test_husk_multi_rop_needs_every_stage_exported(self):
        self.manifest.rops[0].usd_path = "/tmp/a.usd"      # rop2 has none
        code, _, err = self._render(["--engine", "husk", "--all-rops"])
        self.assertEqual(code, 3)
        self.assertIn("/stage/usdrender_rop2", err)

    def test_multi_rop_jobs_carry_their_rop_as_task_id(self):
        args = build_parser().parse_args(["render", "s.hip"])
        jobs, err = cli_mod._render_jobs_for_rop(
            args, self.manifest, self.manifest.rops[0], "hython",
            None, None, tag_task=True)
        self.assertIsNone(err)
        self.assertTrue(all(j.task_id == "/stage/usdrender_rop1" for j in jobs))
        # A single-ROP render keeps today's untagged jobs and output format.
        jobs, _ = cli_mod._render_jobs_for_rop(
            args, self.manifest, self.manifest.rops[0], "hython",
            None, None, tag_task=False)
        self.assertTrue(all(j.task_id == "" for j in jobs))


class TestBatchCli(unittest.TestCase):
    """`hsl batch` renders several scenes back to back, reading them all
    before rendering anything."""

    def setUp(self):
        self._real_inspect = bridge.inspect_hip
        self._real_load = bridge.load_cached
        self._real_save = bridge.save_cached
        bridge.load_cached = lambda hip: None
        bridge.save_cached = lambda m: ""
        self.addCleanup(self._restore)

    def _restore(self):
        bridge.inspect_hip = self._real_inspect
        bridge.load_cached = self._real_load
        bridge.save_cached = self._real_save

    def _manifest_for(self, hip, rop_paths=None):
        paths = rop_paths if rop_paths is not None else ["/stage/usdrender_rop1"]
        return SceneManifest(hip_path=hip, rops=[
            RenderRop(node_path=p, node_type="usdrender_rop",
                      frame_start=1, frame_end=1) for p in paths])

    def _batch(self, argv):
        args = build_parser().parse_args(argv)
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = cli_mod.cmd_batch(args)
        return code, out.getvalue(), err.getvalue()

    def _run_with_fake_queue(self, argv):
        """Run a real (non-dry) batch against a queue stub, capturing jobs."""
        captured = {}

        class FakeQueue:
            def __init__(self, jobs, max_parallel=1, on_event=None):
                captured["jobs"] = jobs
                captured["parallel"] = max_parallel
                self.warnings = []
                self.tasks = []

            def start(self, block=False):
                pass

        original = cli_mod.RenderQueue
        cli_mod.RenderQueue = FakeQueue
        try:
            code, _, _ = self._batch(argv)
        finally:
            cli_mod.RenderQueue = original
        return code, captured

    def test_scenes_render_in_the_order_given(self):
        bridge.inspect_hip = lambda hip, **k: self._manifest_for(hip)
        code, out, err = self._batch(["batch", "a.hip", "b.hip", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertLess(out.index("a.hip"), out.index("b.hip"))
        self.assertIn("2 scene(s)", err)

    def test_a_bad_scene_stops_the_batch_before_anything_renders(self):
        # A typo in scene 3 must surface before scenes 1-2 spend hours
        # rendering -- every scene is read up front.
        calls = []

        def fake_inspect(hip, **k):
            calls.append(hip)
            if hip == "b.hip":
                raise bridge.InspectError("no such .hip")
            return self._manifest_for(hip)

        bridge.inspect_hip = fake_inspect
        code, out, err = self._batch(["batch", "a.hip", "b.hip", "c.hip",
                                      "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("b.hip", err)
        self.assertNotIn("c.hip", calls)
        self.assertEqual(out, "")

    def test_a_scene_with_no_render_rops_is_an_error(self):
        def fake_inspect(hip, **k):
            manifest = self._manifest_for(hip)
            if hip == "b.hip":
                manifest.rops = []
            return manifest

        bridge.inspect_hip = fake_inspect
        code, _, err = self._batch(["batch", "a.hip", "b.hip", "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("b.hip", err)

    def test_task_ids_carry_the_hip_name(self):
        # Node paths repeat across scenes; without the hip prefix two scenes'
        # /stage/usdrender_rop1 chunks would be indistinguishable in a queue.
        bridge.inspect_hip = lambda hip, **k: self._manifest_for(hip)
        code, captured = self._run_with_fake_queue(["batch", "a.hip", "b.hip"])
        self.assertEqual(code, 0)
        self.assertEqual([j.task_id for j in captured["jobs"]],
                         ["a.hip:/stage/usdrender_rop1",
                          "b.hip:/stage/usdrender_rop1"])

    def test_frames_override_applies_to_every_scene(self):
        bridge.inspect_hip = lambda hip, **k: self._manifest_for(hip)
        code, captured = self._run_with_fake_queue(
            ["batch", "a.hip", "b.hip", "--frames", "5-6"])
        self.assertEqual(code, 0)
        chunks = [j.chunk for j in captured["jobs"]]
        self.assertTrue(all(c.start == 5 and c.count == 2 for c in chunks))

    def test_husk_batch_requires_exported_stages(self):
        bridge.inspect_hip = lambda hip, **k: self._manifest_for(hip)
        code, _, err = self._batch(["batch", "a.hip", "--engine", "husk",
                                    "--dry-run"])
        self.assertEqual(code, 3)
        self.assertIn("a.hip", err)

    def test_husk_batch_narrows_the_export_to_the_requested_frames(self):
        # The export genuinely narrows now (UNVERIFIED C8); a husk batch that
        # did not forward --frames would render frames the USD does not carry.
        captured = {}

        def fake_inspect(hip, **k):
            captured[hip] = k
            manifest = self._manifest_for(hip)
            manifest.rops[0].usd_path = "/tmp/%s.usd" % hip
            return manifest

        bridge.inspect_hip = fake_inspect
        code, _, _ = self._batch(["batch", "a.hip", "--engine", "husk",
                                  "--frames", "5-6", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(captured["a.hip"]["export_frames"], (5, 6, 1))
        # And a hython batch, which exports nothing, passes none.
        captured.clear()
        code, _, _ = self._batch(["batch", "a.hip", "--frames", "5-6",
                                  "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIsNone(captured["a.hip"]["export_frames"])

    def test_live_volumes_block_a_husk_batch(self):
        def fake_inspect(hip, **k):
            manifest = self._manifest_for(hip)
            manifest.rops[0].usd_path = "/tmp/a.usd"
            manifest.live_volumes = [LiveVolume(prim_path="/v", field_count=2)]
            return manifest

        bridge.inspect_hip = fake_inspect
        code, _, err = self._batch(["batch", "a.hip", "--engine", "husk",
                                    "--dry-run"])
        self.assertEqual(code, 3)
        self.assertIn("hython", err)

    def test_no_rop_flag_renders_every_rop_of_every_scene(self):
        # Byte-identical to today's behaviour: with no --rop, filtering never
        # runs at all. Two multi-ROP scenes prove every ROP of every scene
        # still makes it into the queue, in manifest order -- the same
        # assertion shape as test_task_ids_carry_the_hip_name.
        bridge.inspect_hip = lambda hip, **k: self._manifest_for(
            hip, ["/stage/usdrender_rop1", "/stage/usdrender_rop2"])
        code, captured = self._run_with_fake_queue(["batch", "a.hip", "b.hip"])
        self.assertEqual(code, 0)
        self.assertEqual(
            [j.task_id for j in captured["jobs"]],
            ["a.hip:/stage/usdrender_rop1", "a.hip:/stage/usdrender_rop2",
             "b.hip:/stage/usdrender_rop1", "b.hip:/stage/usdrender_rop2"])

    def test_bare_rop_spec_filters_every_scene_that_has_it(self):
        # A bare SPEC carries no scene qualifier, so it applies everywhere --
        # including two scenes that happen to share the same default ROP
        # name, which is the common case Houdini's own node naming produces.
        bridge.inspect_hip = lambda hip, **k: self._manifest_for(
            hip, ["/stage/usdrender_rop1", "/stage/usdrender_rop2"])
        code, captured = self._run_with_fake_queue(
            ["batch", "a.hip", "b.hip", "--rop", "/stage/usdrender_rop1"])
        self.assertEqual(code, 0)
        self.assertEqual(
            [j.task_id for j in captured["jobs"]],
            ["a.hip:/stage/usdrender_rop1", "b.hip:/stage/usdrender_rop1"])

    def test_qualified_rop_spec_picks_a_different_rop_per_scene(self):
        # Each scene keeps only the ROP its own qualified SPEC names -- two
        # scenes can render two different passes in the same batch run.
        def fake_inspect(hip, **k):
            return self._manifest_for(
                hip, ["/stage/usdrender_beauty", "/stage/usdrender_fx"])

        bridge.inspect_hip = fake_inspect
        code, captured = self._run_with_fake_queue([
            "batch", "a.hip", "b.hip",
            "--rop", "a.hip:/stage/usdrender_beauty",
            "--rop", "b.hip:/stage/usdrender_fx",
        ])
        self.assertEqual(code, 0)
        self.assertEqual(
            [j.task_id for j in captured["jobs"]],
            ["a.hip:/stage/usdrender_beauty", "b.hip:/stage/usdrender_fx"])

    def test_qualifier_matches_the_scene_name_with_or_without_extension(self):
        # A qualifier may name the scene by its argv basename or that
        # basename with the .hip stripped -- both must resolve to the same
        # scene, case-insensitively (this is Windows).
        bridge.inspect_hip = lambda hip, **k: self._manifest_for(
            hip, ["/stage/rop1", "/stage/rop2", "/stage/rop3"])
        code, captured = self._run_with_fake_queue([
            "batch", "shotA.hip",
            "--rop", "SHOTA:/stage/rop1",       # no extension, upper-case
            "--rop", "shotA.hip:/stage/rop2",   # extension, matching case
        ])
        self.assertEqual(code, 0)
        self.assertEqual(
            [j.task_id for j in captured["jobs"]],
            ["shotA.hip:/stage/rop1", "shotA.hip:/stage/rop2"])

    def test_a_scene_left_with_no_rops_after_filtering_is_an_error(self):
        # A scene whose ROPs the given --rop SPECs happen not to name would
        # otherwise render nothing with no explanation. Name the scene and
        # list what it actually contains, so the fix is obvious.
        def fake_inspect(hip, **k):
            paths = (["/stage/usdrender_rop1"] if hip == "a.hip"
                     else ["/stage/other_rop"])
            return self._manifest_for(hip, paths)

        bridge.inspect_hip = fake_inspect
        code, _, err = self._batch(["batch", "a.hip", "b.hip",
                                    "--rop", "/stage/usdrender_rop1",
                                    "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("b.hip", err)
        self.assertIn("/stage/other_rop", err)

    def test_a_rop_spec_matching_nothing_anywhere_is_an_error(self):
        # A typo in a --rop path or qualifier must not silently render
        # everything (the filter never applied) or nothing (mistaken for a
        # real scene) -- it has to be reported by name.
        bridge.inspect_hip = lambda hip, **k: self._manifest_for(hip)
        code, _, err = self._batch(["batch", "a.hip",
                                    "--rop", "/stage/does_not_exist",
                                    "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("/stage/does_not_exist", err)


class TestReleaseBundle(unittest.TestCase):
    """The standalone zip's correctness rests on the ._pth rewrite and the
    launchers preferring the bundled runtime -- both testable without the
    network the real build needs."""

    # Verbatim shape of the file inside python.org's embeddable zip.
    EMBED_PTH = ("python311.zip\n.\n\n"
                 "# Uncomment to run site.main() automatically\n"
                 "#import site\n")

    @classmethod
    def setUpClass(cls):
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "scripts", "make_release.py")
        spec = importlib.util.spec_from_file_location("make_release", path)
        cls.mr = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mr)

    def test_pth_gains_site_packages_app_root_and_site(self):
        lines = self.mr.patched_pth(self.EMBED_PTH).splitlines()
        self.assertIn("python311.zip", lines)      # stdlib zip kept
        self.assertIn(".", lines)                  # DLL directory kept
        self.assertIn("Lib\\site-packages", lines)  # where PySide6 is seeded
        self.assertIn("..", lines)                 # app root: -m hsl.cli
        self.assertIn("import site", lines)        # shipped commented out
        self.assertNotIn("#import site", lines)

    def test_pth_patch_is_idempotent(self):
        # A cached runtime gets patched again on rebuild; twice must equal once.
        once = self.mr.patched_pth(self.EMBED_PTH)
        self.assertEqual(once, self.mr.patched_pth(once))

    def test_the_full_zip_demands_the_runtime_and_qt(self):
        # A standalone zip missing these is a support ticket; the build must
        # refuse to write it rather than ship it.
        self.assertIn("python/python.exe", self.mr.FULL_REQUIRED)
        self.assertTrue(any("PySide6" in name for name in self.mr.FULL_REQUIRED))

    def test_launchers_prefer_the_bundled_runtime(self):
        # Both .bats must check python\python.exe before running anything --
        # otherwise the standalone zip silently depends on the machine's own
        # Python, which is the exact failure it exists to remove. Read as
        # ASCII: cmd.exe's default codepage mangles anything beyond it.
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for rel in ("launch_ui.bat", os.path.join("bin", "hsl.bat")):
            with io.open(os.path.join(root, rel), encoding="ascii") as fh:
                text = fh.read()
            self.assertIn("python\\python.exe", text, rel)
            self.assertLess(text.index("python\\python.exe"),
                            text.index("-m hsl."), rel)


class TestQueueMemoryMeasurement(unittest.TestCase):
    """The queue measures what each render process actually used. Every
    measurement is optional and must never change how a render behaves."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.fake = os.path.join(self.dir, "fake_husk.py")
        with open(self.fake, "w") as fh:
            fh.write("import sys\nprint('ALF_PROGRESS 100%', flush=True)\n"
                     "sys.exit(9 if '--make-it-fail' in sys.argv else 0)\n")
        if os.name == "nt":
            self.exe = os.path.join(self.dir, "fake_husk.bat")
            with open(self.exe, "w") as fh:
                fh.write(f'@echo off\n"{sys.executable}" "{self.fake}" %*\n')
        else:
            self.exe = self.fake
            os.chmod(self.fake, os.stat(self.fake).st_mode | stat.S_IEXEC)

    def _job(self, task_id="", fail=False, depends=()):
        return RenderJob(usd_file=os.path.join(self.dir, "shot.usd"),
                         engine="husk", husk_exe=self.exe,
                         hip_file=os.path.join(self.dir, "shot.hip"),
                         rop_path="/stage/rop1",
                         task_id=task_id, depends_on=list(depends),
                         chunk=FrameChunk(1, 2, 1),
                         extra_args=["--make-it-fail"] if fail else [])

    def test_measurement_can_be_switched_off_entirely(self):
        queue = RenderQueue([self._job()], measure_memory=False)
        queue.start(block=True)
        task = queue.tasks[0]
        self.assertIsNone(task.peak_rss)
        self.assertIsNone(task.peak_vram)
        self.assertEqual(queue.warnings, [])

    def test_a_missing_measurement_is_none_never_zero(self):
        # None means "nobody measured this"; 0 would be a claim that the
        # render used no memory, which is never true.
        task = Task(job=self._job())
        self.assertIsNone(task.peak_rss)
        self.assertIsNone(task.peak_vram)
        self.assertFalse(task.vram_sampled)

    def test_nothing_is_filed_when_nothing_was_measured(self):
        # An entry carrying no numbers only dilutes the history preflight reads.
        filed = []
        original = memlog.record
        memlog.record = filed.append
        self.addCleanup(setattr, memlog, "record", original)
        queue = RenderQueue([self._job()])
        for task in queue.tasks:            # pretend the platform measured nothing
            task.peak_rss = task.peak_vram = None
        queue._finish_measurement = lambda task: RenderQueue._finish_measurement(
            queue, task)
        queue.start(block=True)
        self.assertTrue(all(s.peak_rss is not None or s.peak_vram is not None
                            for s in filed))

    def test_a_dependency_reports_finishing_before_its_dependant_starts(self):
        """Regression: measuring must not happen between the terminal state
        and the finished event.

        The driver starts dependants the moment it sees a task's *state*, so
        slow work in that window let a dependant start before its prerequisite
        had even reported finishing. Sequencing, not decoration: a consumer
        reading the event stream saw effects before their cause."""
        original = RenderQueue._finish_measurement

        def slow(self, task):               # exaggerate the window
            time.sleep(0.25)
            return original(self, task)

        RenderQueue._finish_measurement = slow
        self.addCleanup(setattr, RenderQueue, "_finish_measurement", original)

        seq = []
        lock = threading.Lock()

        def on_event(event, *payload):
            if event in ("task_started", "task_finished"):
                with lock:
                    seq.append((event, payload[0].job.task_id))

        # max_parallel must leave a free slot: at 1 the semaphore serialises
        # the two anyway (it is released only after the finished event) and
        # the race is invisible. This test passed against the broken code
        # until that was fixed.
        queue = RenderQueue([self._job(task_id="up"),
                             self._job(task_id="down", depends=("up",))],
                            max_parallel=2, on_event=on_event,
                            record_memory=False)
        queue.start(block=True)
        self.assertLess(seq.index(("task_finished", "up")),
                        seq.index(("task_started", "down")))

    def test_a_failed_render_is_still_measured(self):
        # A job killed by the machine running out of memory is the single most
        # useful sample there is, so failure must not skip measurement.
        seen = []
        original = RenderQueue._finish_measurement
        RenderQueue._finish_measurement = lambda self, task: seen.append(task.state)
        self.addCleanup(setattr, RenderQueue, "_finish_measurement", original)
        queue = RenderQueue([self._job(fail=True)])
        queue.start(block=True)
        self.assertEqual(queue.tasks[0].state, State.FAILED)
        self.assertEqual(len(seen), 1)

    def test_a_cancelled_render_is_not_measured(self):
        # We cut it short, so its peak says nothing about what the scene needs.
        seen = []
        original = RenderQueue._finish_measurement
        RenderQueue._finish_measurement = lambda self, task: seen.append(task)
        self.addCleanup(setattr, RenderQueue, "_finish_measurement", original)
        queue = RenderQueue([self._job()])
        queue._cancel.set()
        queue.start(block=True)
        self.assertEqual(seen, [])


class TestMemoryReport(unittest.TestCase):
    """`hsl memory` reports measurements, and says so."""

    def _run(self, argv):
        args = build_parser().parse_args(argv)
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = cli_mod.cmd_memory(args)
        return code, out.getvalue(), err.getvalue()

    def test_unmeasured_bytes_read_unknown_not_zero(self):
        self.assertEqual(sysinfo.human_bytes(None), "unknown")
        self.assertEqual(sysinfo.human_bytes(0), "unknown")
        self.assertEqual(sysinfo.human_bytes(2 * 1024 ** 3), "2.0 GB")

    def test_an_empty_history_explains_itself(self):
        # Silence would read as a broken feature; this is a new install.
        code, out, _ = self._run(["memory"])
        self.assertEqual(code, 0)
        self.assertIn("Nothing measured yet", out)

    def test_the_report_says_these_are_measurements(self):
        memlog.record(memlog.MemorySample(
            hip_path="/jobs/shot.hip", rop_path="/stage/rop1", engine="hython",
            frames=10, peak_rss=8 * 1024 ** 3, when=time.time()))
        code, out, _ = self._run(["memory"])
        self.assertEqual(code, 0)
        self.assertIn("8.0 GB", out)
        self.assertIn("not predictions", out)

    def test_vram_from_polling_is_labelled_sampled(self):
        # A polled figure can miss a spike; it must not read as exact.
        memlog.record(memlog.MemorySample(
            hip_path="/jobs/shot.hip", rop_path="/stage/rop1",
            peak_rss=1024 ** 3, peak_vram=4 * 1024 ** 3, vram_sampled=True,
            when=time.time()))
        code, out, _ = self._run(["memory"])
        self.assertIn("sampled", out)

    def test_machine_report_distinguishes_no_gpu_from_no_tooling(self):
        code, out, _ = self._run(["memory", "--machine"])
        self.assertEqual(code, 0)
        self.assertIn("installed RAM", out)
        self.assertIn("per-render VRAM", out)

    def test_forget_clears_the_history(self):
        memlog.record(memlog.MemorySample(hip_path="/jobs/shot.hip",
                                          peak_rss=1024 ** 3, when=time.time()))
        code, out, _ = self._run(["memory", "--forget"])
        self.assertEqual(code, 0)
        self.assertEqual(memlog.history(), [])


class TestConsoleEncoding(unittest.TestCase):
    """A fresh Windows console is cp1252. Printing a character it cannot map
    raises UnicodeEncodeError half way through a report -- the user loses the
    output and gets a traceback about a decoration."""

    def test_cli_prints_nothing_a_cp1252_console_would_choke_on(self):
        offenders = {}
        for name in ("cli.py", "husk.py", "runner.py", "bridge.py",
                     "preflight.py", "inspector.py"):
            path = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "hsl", name)
            with io.open(path, encoding="utf-8") as fh:
                for number, line in enumerate(fh, 1):
                    for char in line:
                        if ord(char) < 128:
                            continue
                        try:
                            char.encode("cp1252")
                        except UnicodeEncodeError:
                            offenders.setdefault(
                                f"{name}:{number}", set()).add(hex(ord(char)))
        self.assertEqual(offenders, {},
                         "these would crash `hsl inspect` on a default "
                         "Windows console; use an ASCII equivalent")

    def test_making_the_console_safe_never_raises(self):
        """Called on every run, including when stdout is a pipe or a StringIO."""
        original = sys.stdout
        try:
            sys.stdout = io.StringIO()
            cli_mod._make_console_safe()
        finally:
            sys.stdout = original


class TestCookCommands(unittest.TestCase):
    """Cooking a cache is not rendering: it goes to a different inspector
    entry point and must not carry render-only flags."""

    def _task(self, **kw):
        base = dict(node_path="/obj/geo1/filecache1", use_frame_range=True,
                    frame_start=1, frame_end=10, frame_inc=1, kind=TASK_CACHE)
        base.update(kw)
        return OutputTask(**base)

    def _cmd(self, task, **over):
        manifest = SceneManifest(hip_path="/jobs/shot.hip")
        jobs = jobs_for_task(manifest, task, hython_exe="hython", **over)
        return build_command(jobs[0]), jobs[0]

    def test_a_cache_job_uses_the_cook_entry_point(self):
        cmd, _ = self._cmd(self._task())
        self.assertIn("--cook", cmd)
        self.assertNotIn("--render-direct", cmd)
        self.assertIn("--rop", cmd)
        self.assertEqual(cmd[cmd.index("--rop") + 1], "/obj/geo1/filecache1")

    def _solaris_manifest(self):
        """A manifest where the task IS a described Solaris render ROP."""
        manifest = SceneManifest(hip_path="/jobs/shot.hip",
                                 default_settings_prim="/Render/rs")
        manifest.rops = [RenderRop(
            node_path="/stage/usdrender_rop1", node_type="usdrender_rop",
            renderer="BRAY_HdKarmaXPU", settings_prim="/Render/rs",
            camera="/cameras/shotcam", frame_start=1, frame_end=4,
            use_frame_range=True)]
        manifest.tasks = [OutputTask(
            node_path="/stage/usdrender_rop1", node_type="usdrender_rop",
            kind=TASK_RENDER, frame_start=1, frame_end=4, use_frame_range=True)]
        return manifest

    def test_a_solaris_render_still_goes_to_render_direct(self):
        manifest = self._solaris_manifest()
        jobs = jobs_for_task(manifest, manifest.tasks[0], hython_exe="hython")
        cmd = build_command(jobs[0])
        self.assertIn("--render-direct", cmd)
        self.assertNotIn("--cook", cmd)

    def test_a_solaris_render_keeps_the_settings_the_scene_asked_for(self):
        """Rebuilding the job here instead of deferring to jobs_for_rop lost
        the renderer, camera and settings prim -- so the render silently ran
        with bare Karma defaults and no camera."""
        manifest = self._solaris_manifest()
        job = jobs_for_task(manifest, manifest.tasks[0], hython_exe="hython")[0]
        self.assertEqual(job.renderer, "BRAY_HdKarmaXPU")
        self.assertEqual(job.camera, "/cameras/shotcam")
        self.assertEqual(job.settings_prim, "/Render/rs")
        # ...and it is still identified as part of its task.
        self.assertEqual(job.task_id, "/stage/usdrender_rop1")

    def test_a_render_rop_usd_cannot_drive_is_cooked_instead(self):
        """Mantra and OpenGL are renders, but --render-direct resolves its node
        through find_render_rops(), which only knows USD ones -- so sending
        them there would fail to find the node at all."""
        cmd, job = self._cmd(self._task(kind=TASK_RENDER,
                                        node_path="/out/mantra1"))
        self.assertIn("--cook", cmd)
        self.assertNotIn("--render-direct", cmd)
        self.assertTrue(job.cook)

    def test_a_simulation_asks_for_an_ordered_single_call(self):
        cmd, job = self._cmd(self._task(kind=TASK_SIM))
        self.assertTrue(job.sequential)
        self.assertIn("--sequential", cmd)
        # ...and the whole range went into one chunk.
        self.assertEqual(cmd[cmd.index("--frame-count") + 1], "10")

    def test_a_plain_cache_is_not_marked_sequential(self):
        cmd, _ = self._cmd(self._task(kind=TASK_CACHE))
        self.assertNotIn("--sequential", cmd)

    def test_render_only_flags_are_left_off_a_cook(self):
        """A renderer or resolution on a geometry cache is meaningless, and a
        stray --output would repoint the cache."""
        cmd, _ = self._cmd(self._task(), renderer="BRAY_HdKarma",
                           camera="/cameras/cam1", resolution=(960, 540))
        for flag in ("--renderer", "--camera", "--res", "--settings"):
            self.assertNotIn(flag, cmd)

    def test_an_explicit_output_still_reaches_the_cook(self):
        cmd, _ = self._cmd(self._task(), output="/jobs/out/geo.$F4.bgeo.sc")
        self.assertIn("--output", cmd)
        self.assertEqual(cmd[cmd.index("--output") + 1], "/jobs/out/geo.$F4.bgeo.sc")

    def test_frame_tokens_survive_into_expected_outputs(self):
        """The template must stay a template -- evaluating it at scan time
        would claim every frame writes to frame 1's file."""
        _, job = self._cmd(self._task(outputs=["/jobs/geo.$F4.bgeo.sc"]))
        self.assertEqual(job.expected_outputs, ["/jobs/geo.$F4.bgeo.sc"])
        self.assertTrue(has_unexpanded_tokens(job.expected_outputs[0]))


class TestQueueDependencies(unittest.TestCase):
    """Dependency ordering, skipping and cycle rejection, against a fake binary.

    Every assertion here is on scheduling *decisions* rather than wall-clock
    overlap, so none of it depends on how fast the machine spawns processes.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        script = os.path.join(self.dir, "fake_rop.py")
        with open(script, "w") as fh:
            fh.write(
                "import sys, time\n"
                "time.sleep(0.03)\n"
                "print('ALF_PROGRESS 100%', flush=True)\n"
                "sys.exit(7 if '--make-it-fail' in sys.argv else 0)\n"
            )
        if os.name == "nt":
            self.exe = os.path.join(self.dir, "fake_rop.bat")
            with open(self.exe, "w") as fh:
                fh.write(f'@echo off\n"{sys.executable}" "{script}" %*\n')
        else:
            os.chmod(script, os.stat(script).st_mode | stat.S_IEXEC)
            self.exe = script

    def job(self, task_id, depends_on=(), fail=False, start=1):
        return RenderJob(
            usd_file=os.path.join(self.dir, "shot.usd"),
            engine="husk",                  # the fake binary stands in for husk
            husk_exe=self.exe,
            chunk=FrameChunk(start, 1, 1),
            task_id=task_id,
            depends_on=list(depends_on),
            extra_args=["--make-it-fail"] if fail else [],
        )

    def _run(self, jobs, **kwargs):
        events = []
        queue = RenderQueue(jobs, on_event=lambda *a: events.append(a), **kwargs)
        queue.start(block=True)
        seq = [(name, payload[0].job.task_id) for name, *payload in events
               if name in ("task_started", "task_finished")]
        return queue, seq

    def test_a_dependant_starts_only_after_its_prerequisite_finishes(self):
        # The dependant is listed *first*, so an in-order scheduler fails this.
        queue, seq = self._run([
            self.job("/out/sim", depends_on=["/out/cache"]),
            self.job("/out/cache"),
        ], max_parallel=4)

        self.assertTrue(all(t.state is State.DONE for t in queue.tasks))
        self.assertLess(seq.index(("task_finished", "/out/cache")),
                        seq.index(("task_started", "/out/sim")))

    def test_a_dependant_waits_for_every_chunk_of_its_prerequisite(self):
        """Half a cache is not an input anything downstream can use."""
        queue, seq = self._run([
            self.job("/out/cache", start=1),
            self.job("/out/cache", start=2),
            self.job("/out/cache", start=3),
            self.job("/out/sim", depends_on=["/out/cache"]),
        ], max_parallel=4)

        self.assertTrue(all(t.state is State.DONE for t in queue.tasks))
        last_chunk = max(i for i, entry in enumerate(seq)
                         if entry == ("task_finished", "/out/cache"))
        self.assertLess(last_chunk, seq.index(("task_started", "/out/sim")))

    def test_diamond_dependencies_resolve(self):
        queue, seq = self._run([
            self.job("/out/d", depends_on=["/out/b", "/out/c"]),
            self.job("/out/b", depends_on=["/out/a"]),
            self.job("/out/c", depends_on=["/out/a"]),
            self.job("/out/a"),
        ], max_parallel=4)

        self.assertTrue(all(t.state is State.DONE for t in queue.tasks))
        start_d = seq.index(("task_started", "/out/d"))
        for upstream in ("/out/a", "/out/b", "/out/c"):
            self.assertLess(seq.index(("task_finished", upstream)), start_d)

    def test_dependants_are_skipped_when_a_prerequisite_fails(self):
        queue, _ = self._run([
            self.job("/out/cache", fail=True),
            self.job("/out/sim", depends_on=["/out/cache"]),
        ])
        states = {t.job.task_id: t.state for t in queue.tasks}
        self.assertIs(states["/out/cache"], State.FAILED)
        self.assertIs(states["/out/sim"], State.SKIPPED)
        self.assertTrue(queue.finished)
        skipped = next(t for t in queue.tasks if t.state is State.SKIPPED)
        self.assertTrue(any("depends on" in line for line in skipped.log))

    def test_a_skipped_task_does_not_strand_the_queue(self):
        """A skip is terminal, so the bar reaches 100 and the queue ends."""
        queue, _ = self._run([
            self.job("/out/a", fail=True),
            self.job("/out/b", depends_on=["/out/a"]),
        ])
        self.assertEqual(queue.progress, 100)

    def test_a_dependency_cycle_is_refused_before_anything_runs(self):
        with self.assertRaises(ValueError) as ctx:
            RenderQueue([self.job("/out/a", depends_on=["/out/b"]),
                         self.job("/out/b", depends_on=["/out/a"])])
        message = str(ctx.exception)
        self.assertIn("cycle", message)
        self.assertIn("/out/a", message)

    def test_an_unknown_dependency_runs_anyway_and_says_so(self):
        """Rendering one ROP out of a scene legitimately leaves its upstream
        out of the queue -- that must not deadlock, but must not be silent."""
        queue, _ = self._run([self.job("/out/sim", depends_on=["/out/absent"])])
        self.assertIs(queue.tasks[0].state, State.DONE)
        self.assertTrue(any("not in this queue" in w for w in queue.warnings))

    def test_jobs_without_task_ids_behave_exactly_as_before(self):
        """The Solaris path sets neither field; order must be untouched."""
        jobs = [RenderJob(usd_file=os.path.join(self.dir, "shot.usd"),
                          engine="husk", husk_exe=self.exe,
                          chunk=FrameChunk(n, 1, 1)) for n in (1, 2, 3)]
        queue, seq = self._run(jobs, max_parallel=1)
        self.assertTrue(all(t.state is State.DONE for t in queue.tasks))
        self.assertEqual([name for name, _ in seq].count("task_started"), 3)
        self.assertEqual(queue.warnings, [])


class TestProgressText(unittest.TestCase):
    """hsl.progress -- the queue-table/progress-bar text, extracted out of
    ui.py so it can be tested without Qt installed (AGENTS.md non-negotiable
    #5). Fabricated Task/RenderJob/FrameChunk objects, no subprocess, no
    RenderQueue -- these functions only ever read task.state/.progress/
    .job.chunk/.duration.
    """

    def task(self, start, count, state, progress=0, started_at=0.0,
            finished_at=0.0, inc=1):
        return Task(
            job=RenderJob(chunk=FrameChunk(start, count, inc)),
            state=state, progress=progress,
            started_at=started_at, finished_at=finished_at,
        )

    # -- format_eta ---------------------------------------------------

    def test_format_eta_under_a_minute(self):
        self.assertEqual(format_eta(9), "0:09")

    def test_format_eta_minutes_and_seconds(self):
        self.assertEqual(format_eta(65), "1:05")

    def test_format_eta_rolls_over_to_hours(self):
        self.assertEqual(format_eta(3725), "1:02:05")

    # -- eta_seconds ----------------------------------------------------

    def test_eta_is_none_until_a_chunk_has_finished(self):
        """Nothing has completed yet, so there is no observed rate to use --
        the render might just be starting, or ten hours in; guessing which
        would be exactly the invented number this module refuses to show."""
        tasks = [
            self.task(1, 5, State.RUNNING, progress=40, started_at=100.0),
            self.task(6, 5, State.PENDING),
        ]
        self.assertIsNone(eta_seconds(tasks, done_frames=0, total_frames=10))

    def test_eta_derived_from_a_finished_chunk_s_observed_rate(self):
        """One chunk of 5 frames took exactly 50s -- 10s/frame -- with 5
        frames left, that is a concrete, checkable 50s, not a guess."""
        tasks = [
            self.task(1, 5, State.DONE, progress=100,
                     started_at=1000.0, finished_at=1050.0),
            self.task(6, 5, State.PENDING),
        ]
        seconds = eta_seconds(tasks, done_frames=5, total_frames=10)
        self.assertEqual(seconds, 50.0)
        self.assertEqual(format_eta(seconds), "0:50")

    def test_eta_ignores_a_still_running_chunk_s_partial_time(self):
        """A chunk that has not finished has no wall-clock duration to trust
        yet -- only a finished chunk's start-to-finish time counts."""
        tasks = [
            self.task(1, 5, State.DONE, progress=100,
                     started_at=500.0, finished_at=550.0),
            self.task(6, 5, State.RUNNING, progress=90, started_at=9999.0),
        ]
        seconds = eta_seconds(tasks, done_frames=5, total_frames=10)
        self.assertEqual(seconds, 50.0)      # unaffected by the running task

    # -- task_progress_text ----------------------------------------------

    def test_pending_task_shows_a_dash(self):
        t = self.task(1, 1, State.PENDING)
        self.assertEqual(task_progress_text(t, progress_seen=set()), "-")

    def test_no_progress_data_shows_running_not_a_fake_percentage(self):
        """hython never emits ALF_PROGRESS at all, and husk may just not have
        printed its first line yet -- either way, 0% would be a percentage
        that never actually arrived."""
        t = self.task(1, 1, State.RUNNING, progress=0)
        self.assertEqual(task_progress_text(t, progress_seen=set()), "running…")

    def test_single_frame_chunk_shows_its_own_intra_frame_percent(self):
        """A chunk of exactly one frame *is* the frame husk is on, so its
        percentage is that frame's own progress -- worth naming the frame."""
        t = self.task(101, 1, State.RUNNING, progress=47)
        self.assertEqual(
            task_progress_text(t, progress_seen={id(t)}), "frame 101: 47%")

    def test_multi_frame_chunk_labels_the_chunk_not_a_specific_frame(self):
        """husk does not say which of a chunk's several frames it is on, so
        this must not claim to know -- the percentage is the chunk's own."""
        t = self.task(6, 5, State.RUNNING, progress=63)
        self.assertEqual(
            task_progress_text(t, progress_seen={id(t)}), "63% of 5 frames")

    def test_finished_task_shows_its_final_percent(self):
        t = self.task(1, 5, State.DONE, progress=100)
        self.assertEqual(task_progress_text(t, progress_seen=set()), "100%")

    # -- queue_progress_summary -------------------------------------------

    def test_frame_count_only_credits_fully_finished_chunks(self):
        """One chunk of 5 frames finished; a second chunk of 5 is running at
        60%. The frame count must read 5 of 10, not 8 of 10 -- crediting a
        fraction of the running chunk would claim to know which of its
        frames are actually done, which husk never says."""
        tasks = [
            self.task(1, 5, State.DONE, progress=100,
                     started_at=0.0, finished_at=10.0),
            self.task(6, 5, State.RUNNING, progress=60, started_at=9999.0),
        ]
        summary = queue_progress_summary(tasks, percent=55, progress_seen={id(tasks[1])})
        self.assertIn("Frame 5 of 10", summary)
        self.assertNotIn("Frame 8", summary)

    def test_eta_is_omitted_until_something_has_finished(self):
        tasks = [self.task(1, 5, State.RUNNING, progress=10, started_at=1.0)]
        summary = queue_progress_summary(tasks, percent=2, progress_seen={id(tasks[0])})
        self.assertNotIn("ETA", summary)

    def test_summary_includes_eta_from_an_observed_rate(self):
        tasks = [
            self.task(1, 5, State.DONE, progress=100,
                     started_at=1000.0, finished_at=1050.0),
            self.task(6, 5, State.PENDING),
        ]
        summary = queue_progress_summary(tasks, percent=50, progress_seen=set())
        self.assertEqual(summary, "50% - Frame 5 of 10 - ETA 0:50")

    def test_summary_names_the_running_chunk_not_a_frame_when_it_has_several(self):
        tasks = [
            self.task(1, 5, State.DONE, progress=100,
                     started_at=0.0, finished_at=10.0),
            self.task(6, 5, State.RUNNING, progress=63, started_at=9999.0),
        ]
        summary = queue_progress_summary(tasks, percent=68, progress_seen={id(tasks[1])})
        self.assertIn("chunk 6-10: 63%", summary)

    def test_summary_says_nothing_extra_when_no_progress_has_arrived(self):
        """A running task the queue has not heard from yet (hython, or husk
        before its first line) adds no parenthetical detail -- there is
        nothing real to report."""
        tasks = [self.task(1, 5, State.RUNNING, progress=0, started_at=1.0)]
        summary = queue_progress_summary(tasks, percent=0, progress_seen=set())
        self.assertEqual(summary, "0% - Frame 0 of 5")


class TestMemlog(unittest.TestCase):
    """memlog is a JSON-backed store of what past renders actually used.

    LOG_FILE is redirected into a fresh temp directory for every test here --
    this must never touch the real user profile."""

    def setUp(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        original = memlog.LOG_FILE
        memlog.LOG_FILE = os.path.join(tmpdir.name, "memory.json")
        self.addCleanup(setattr, memlog, "LOG_FILE", original)

    def test_a_sample_with_no_measurement_at_all_is_not_recorded(self):
        # An entry that says nothing (no RSS, no VRAM) is noise that would
        # only dilute worst() -- record() must refuse it outright.
        memlog.record(memlog.MemorySample(hip_path="/j/s.hip", rop_path="/out/rop1"))
        self.assertEqual(memlog.history(), [])

    def test_a_sample_with_only_vram_is_still_recorded(self):
        memlog.record(memlog.MemorySample(hip_path="/j/s.hip", rop_path="/out/rop1",
                                          peak_vram=1024, vram_sampled=True))
        self.assertEqual(len(memlog.history()), 1)

    def test_per_key_cap_evicts_oldest_first(self):
        # Seed one key already at the cap, then push it over with one more
        # record() call -- the oldest entry for THIS key must be the one gone.
        seed = [memlog.MemorySample(hip_path="/j/s.hip", rop_path="/out/rop1",
                                    peak_rss=1000 + i, when=float(i))
               for i in range(memlog.MAX_PER_KEY)]
        memlog._save_all(seed)
        memlog.record(memlog.MemorySample(hip_path="/j/s.hip", rop_path="/out/rop1",
                                          peak_rss=9999, when=float(memlog.MAX_PER_KEY)))
        hist = memlog.history("/j/s.hip", "/out/rop1")
        self.assertEqual(len(hist), memlog.MAX_PER_KEY)
        self.assertEqual(hist[0].when, float(memlog.MAX_PER_KEY))  # newest first
        self.assertNotIn(0.0, {s.when for s in hist})              # oldest evicted

    def test_overall_cap_evicts_oldest_first_across_keys(self):
        # Each sample here has its OWN key (distinct hip), so the per-key cap
        # never fires -- only the whole-store cap should trim anything.
        seed = [memlog.MemorySample(hip_path=f"/j/scene{i}.hip", rop_path="/r",
                                    peak_rss=1, when=float(i))
               for i in range(memlog.MAX_TOTAL)]
        memlog._save_all(seed)
        memlog.record(memlog.MemorySample(hip_path="/j/new.hip", rop_path="/r",
                                          peak_rss=1, when=float(memlog.MAX_TOTAL)))
        everything = memlog.history()
        self.assertEqual(len(everything), memlog.MAX_TOTAL)
        self.assertEqual(everything[0].when, float(memlog.MAX_TOTAL))  # newest first
        self.assertNotIn(0.0, {s.when for s in everything})            # oldest evicted

    def test_corrupt_json_yields_empty_history_not_a_raise(self):
        os.makedirs(os.path.dirname(memlog.LOG_FILE), exist_ok=True)
        with open(memlog.LOG_FILE, "w", encoding="utf-8") as fh:
            fh.write("{not valid json at all")
        self.assertEqual(memlog.history(), [])

    def test_unreadable_store_degrades_to_empty_history(self):
        # A missing file must behave the same as a corrupt one -- no raise.
        self.assertFalse(os.path.exists(memlog.LOG_FILE))
        self.assertEqual(memlog.history(), [])
        self.assertIsNone(memlog.worst("/j/s.hip"))

    def test_differently_spelled_hip_path_still_matches_the_same_key(self):
        memlog.record(memlog.MemorySample(
            hip_path="/jobs/shot/Shot.hip", rop_path="/out/rop1", peak_rss=2000))
        # Different case and a relative-looking form must resolve to the
        # SAME key as the originally-recorded absolute, mixed-case path.
        hits = memlog.history("/JOBS/shot/shot.hip", "/out/rop1")
        self.assertEqual(len(hits), 1)
        # ... but the sample keeps its ORIGINAL spelling for display.
        self.assertEqual(hits[0].hip_path, "/jobs/shot/Shot.hip")

    def test_worst_picks_the_highest_peak_not_the_newest(self):
        memlog.record(memlog.MemorySample(hip_path="/s.hip", rop_path="/r",
                                          peak_rss=1000, when=1.0))
        memlog.record(memlog.MemorySample(hip_path="/s.hip", rop_path="/r",
                                          peak_rss=5000, when=2.0))
        memlog.record(memlog.MemorySample(hip_path="/s.hip", rop_path="/r",
                                          peak_rss=3000, when=3.0))
        worst = memlog.worst("/s.hip", "/r")
        self.assertEqual(worst.peak_rss, 5000)
        self.assertEqual(worst.when, 2.0)

    def test_worst_ignores_samples_with_no_rss_measurement(self):
        # A VRAM-only sample cannot answer "highest peak_rss" -- it must be
        # skipped rather than compared as if its peak_rss were 0.
        memlog.record(memlog.MemorySample(hip_path="/s.hip", rop_path="/r",
                                          peak_vram=999999999, vram_sampled=True))
        memlog.record(memlog.MemorySample(hip_path="/s.hip", rop_path="/r",
                                          peak_rss=100, when=1.0))
        worst = memlog.worst("/s.hip", "/r")
        self.assertEqual(worst.peak_rss, 100)

    def test_forget_one_scene_scopes_to_that_hip_only(self):
        memlog.record(memlog.MemorySample(hip_path="/a.hip", rop_path="/r", peak_rss=1))
        memlog.record(memlog.MemorySample(hip_path="/b.hip", rop_path="/r", peak_rss=1))
        removed = memlog.forget("/a.hip")
        self.assertEqual(removed, 1)
        self.assertEqual(memlog.history("/a.hip"), [])
        self.assertEqual(len(memlog.history("/b.hip")), 1)

    def test_forget_with_no_argument_clears_everything(self):
        memlog.record(memlog.MemorySample(hip_path="/a.hip", rop_path="/r", peak_rss=1))
        memlog.record(memlog.MemorySample(hip_path="/b.hip", rop_path="/r", peak_rss=1))
        removed = memlog.forget()
        self.assertEqual(removed, 2)
        self.assertEqual(memlog.history(), [])

    def test_a_store_written_before_whole_tree_measurement_still_loads(self):
        """Additive schema change, the discipline manifest.py documents: an
        old record has no peak_rss_is_tree key at all, and must load with
        every other field intact.

        It defaults to False, which is not merely the safe default but the
        *true* one -- every figure recorded before Job Objects existed here
        was single-process, so the default labels old rows correctly instead
        of promoting them to a claim they cannot support."""
        os.makedirs(os.path.dirname(memlog.LOG_FILE), exist_ok=True)
        with open(memlog.LOG_FILE, "w", encoding="utf-8") as fh:
            json.dump([{"hip_path": "/j/old.hip", "rop_path": "/out/rop1",
                        "engine": "husk", "frames": 4, "peak_rss": 1234,
                        "peak_vram": None, "vram_sampled": False,
                        "when": 10.0, "houdini": "20.5.370"}], fh)
        loaded = memlog.history()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].peak_rss, 1234)
        self.assertEqual(loaded[0].engine, "husk")
        self.assertEqual(loaded[0].houdini, "20.5.370")
        self.assertFalse(loaded[0].peak_rss_is_tree)

    def test_the_whole_tree_flag_survives_a_round_trip(self):
        # It is what tells a reader whether the number can be trusted as the
        # render's total, so losing it in the file would be as bad as never
        # having measured it.
        memlog.record(memlog.MemorySample(hip_path="/j/s.hip", rop_path="/r",
                                          peak_rss=4096, peak_rss_is_tree=True))
        self.assertTrue(memlog.history()[0].peak_rss_is_tree)


class TestPreflightMemory(unittest.TestCase):
    """The memory preflight check only speaks when there is a real
    measurement, and it must always say MEASURED, never predicted."""

    def setUp(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        original_log = memlog.LOG_FILE
        memlog.LOG_FILE = os.path.join(tmpdir.name, "memory.json")
        self.addCleanup(setattr, memlog, "LOG_FILE", original_log)

        original_ram = sysinfo.total_ram_bytes
        self.addCleanup(setattr, sysinfo, "total_ram_bytes", original_ram)

    def _set_ram(self, value):
        sysinfo.total_ram_bytes = lambda: value

    def _hits(self, job):
        return [w for w in preflight.run_preflight_checks(job)
               if w.category == "memory"]

    def test_no_history_means_no_memory_check_at_all(self):
        # Silence is correct for a scene that has never been measured --
        # a check that always chatters gets ignored.
        self._set_ram(32 * 1024 ** 3)
        job = RenderJob(hip_file="/jobs/shot.hip", rop_path="/out/rop1")
        self.assertEqual(self._hits(job), [])

    def test_peak_above_installed_ram_is_an_error(self):
        self._set_ram(16 * 1024 ** 3)
        memlog.record(memlog.MemorySample(
            hip_path="/jobs/shot.hip", rop_path="/out/rop1",
            peak_rss=20 * 1024 ** 3, when=1000.0))
        job = RenderJob(hip_file="/jobs/shot.hip", rop_path="/out/rop1")
        hits = self._hits(job)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].level, "error")
        self.assertIn("GB", hits[0].message)
        self.assertIn("measured", hits[0].message.lower())
        self.assertIn("not predicted", hits[0].message.lower())

    def test_peak_at_about_ninety_percent_is_a_warning(self):
        total = 16 * 1024 ** 3
        self._set_ram(total)
        memlog.record(memlog.MemorySample(
            hip_path="/jobs/shot.hip", rop_path="/out/rop1",
            peak_rss=int(total * 0.9), when=1000.0))
        job = RenderJob(hip_file="/jobs/shot.hip", rop_path="/out/rop1")
        hits = self._hits(job)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].level, "warning")
        self.assertIn("not predicted", hits[0].message.lower())

    def test_ram_unknown_still_reports_an_info_line(self):
        # Knowing the recorded peak is useful even with nothing to compare
        # it against.
        self._set_ram(None)
        memlog.record(memlog.MemorySample(
            hip_path="/jobs/shot.hip", rop_path="/out/rop1",
            peak_rss=8 * 1024 ** 3, when=1000.0))
        job = RenderJob(hip_file="/jobs/shot.hip", rop_path="/out/rop1")
        hits = self._hits(job)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].level, "info")
        self.assertIn("GB", hits[0].message)
        self.assertIn("not predicted", hits[0].message.lower())

    def test_peak_well_under_ram_is_an_info_line_naming_the_measurement(self):
        self._set_ram(64 * 1024 ** 3)
        memlog.record(memlog.MemorySample(
            hip_path="/jobs/shot.hip", rop_path="/out/rop1",
            peak_rss=8 * 1024 ** 3, when=1000.0))
        job = RenderJob(hip_file="/jobs/shot.hip", rop_path="/out/rop1")
        hits = self._hits(job)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].level, "info")
        self.assertIn("8.0 GB", hits[0].message)
        self.assertIn("not predicted", hits[0].message.lower())

    def test_check_never_fires_for_a_different_rop_on_the_same_hip(self):
        self._set_ram(4 * 1024 ** 3)
        memlog.record(memlog.MemorySample(
            hip_path="/jobs/shot.hip", rop_path="/out/rop1",
            peak_rss=20 * 1024 ** 3, when=1000.0))
        job = RenderJob(hip_file="/jobs/shot.hip", rop_path="/out/rop2")
        self.assertEqual(self._hits(job), [])

    def test_a_single_process_history_is_called_a_lower_bound(self):
        """This is where a wrongly-small figure does its damage: preflight
        compares the recorded peak against installed RAM and pronounces on
        whether the shot fits. A number that only covered a wrapper script
        must not be presented as the render's total."""
        self._set_ram(64 * 1024 ** 3)
        memlog.record(memlog.MemorySample(
            hip_path="/jobs/shot.hip", rop_path="/out/rop1",
            peak_rss=8 * 1024 ** 2, peak_rss_is_tree=False, when=1000.0))
        job = RenderJob(hip_file="/jobs/shot.hip", rop_path="/out/rop1")
        hits = self._hits(job)
        self.assertEqual(len(hits), 1)
        self.assertIn("lower bound", hits[0].message)
        self.assertIn("wrapper script", hits[0].message)

    def test_a_whole_tree_history_is_reported_without_the_caveat(self):
        # The caveat is only honest when it applies; attaching it to a figure
        # that really did cover the tree would teach people to ignore it.
        self._set_ram(64 * 1024 ** 3)
        memlog.record(memlog.MemorySample(
            hip_path="/jobs/shot.hip", rop_path="/out/rop1",
            peak_rss=8 * 1024 ** 3, peak_rss_is_tree=True, when=1000.0))
        job = RenderJob(hip_file="/jobs/shot.hip", rop_path="/out/rop1")
        hits = self._hits(job)
        self.assertEqual(len(hits), 1)
        self.assertNotIn("lower bound", hits[0].message)
        self.assertIn("not predicted", hits[0].message.lower())


# Captured verbatim from `nvidia-smi --query-compute-apps=pid,used_memory
# --format=csv,noheader,nounits` on a Windows 11 box with an RTX 3090 and an
# RTX 2060 SUPER (driver 610.62, both cards in WDDM mode). Every row says
# [N/A]: the driver will not attribute VRAM per process on consumer cards
# under WDDM. This is the normal case on an artist workstation, not an edge
# case, which is why it gets a fixture of real output rather than a guess.
NVIDIA_SMI_ALL_NA = """\
2284, [N/A]
9268, [N/A]
8892, [N/A]
"""

# The same query on a machine whose driver does report figures.
NVIDIA_SMI_WITH_NUMBERS = """\
4242, 1024
4243, 512
4244, 16384
"""

# Captured verbatim from `nvidia-smi --query-gpu=index,name,memory.total
# --format=csv,noheader,nounits` on that same machine.
NVIDIA_SMI_GPUS = """\
0, NVIDIA GeForce RTX 3090, 24576
1, NVIDIA GeForce RTX 2060 SUPER, 8192
"""


class TestNvidiaSmiParsing(unittest.TestCase):
    """The pure text half of the GPU probe.

    Split out as its own function precisely so it can be tested on a machine
    with no GPU at all: everything that can go wrong with nvidia-smi's *output
    format* is exercised here, on captured text, with no driver involved.
    """

    def test_reads_pids_and_converts_mib_to_bytes(self):
        """nvidia-smi's `nounits` output is MiB, and every consumer of this
        works in bytes. Getting the unit wrong would under-report VRAM by a
        factor of a million."""
        parsed = sysinfo._parse_nvidia_smi(NVIDIA_SMI_WITH_NUMBERS)
        self.assertEqual(parsed, {
            4242: 1024 * 1024 * 1024,
            4243: 512 * 1024 * 1024,
            4244: 16384 * 1024 * 1024,
        })

    def test_na_rows_are_dropped_not_recorded_as_zero(self):
        """The whole point. On Windows consumer cards every row is [N/A].
        Recording those pids as 0 bytes would read downstream as "this render
        used no VRAM" -- a confident, wrong claim. They must be absent, so the
        caller can say "unknown" instead."""
        self.assertEqual(sysinfo._parse_nvidia_smi(NVIDIA_SMI_ALL_NA), {})

    def test_other_driver_refusals_are_dropped_too(self):
        """[N/A] is not the only thing a driver prints instead of a number."""
        text = ("11, [Not Supported]\n"
                "12, [Insufficient Permissions]\n"
                "13, N/A\n"
                "14, [Unknown Error]\n")
        self.assertEqual(sysinfo._parse_nvidia_smi(text), {})

    def test_a_good_row_survives_alongside_refused_ones(self):
        """A mixed machine must not lose the readings it does have."""
        text = "11, [N/A]\n22, 256\n33, [N/A]\n"
        self.assertEqual(sysinfo._parse_nvidia_smi(text), {22: 256 * 1024 * 1024})

    def test_a_pid_on_two_gpus_is_summed(self):
        """nvidia-smi emits one row per (process, card). A render using both
        cards must report its total, not whichever row happened to be last."""
        text = "500, 1024\n500, 2048\n"
        self.assertEqual(sysinfo._parse_nvidia_smi(text),
                         {500: 3072 * 1024 * 1024})

    def test_empty_and_malformed_text_yields_no_rows(self):
        """A driver error, a truncated pipe or a changed output format must
        produce nothing rather than an exception in the middle of a render."""
        for text in ("", "\n\n", "garbage", "no-comma-here", "  , 12",
                     "12", "abc, def"):
            with self.subTest(text=text):
                self.assertEqual(sysinfo._parse_nvidia_smi(text), {})

    def test_none_is_tolerated(self):
        """_run_nvidia_smi returns "" on failure, but a None must not blow up
        the parser either -- this runs inside a render loop."""
        self.assertEqual(sysinfo._parse_nvidia_smi(None), {})

    def test_nonsense_pids_are_rejected(self):
        """A pid of 0 or a negative one is not a process; keeping it would put
        a bogus key in a dict callers look their own pid up in."""
        self.assertEqual(sysinfo._parse_nvidia_smi("0, 128\n-5, 128\n"), {})

    def test_gpu_listing_reads_names_and_total_vram(self):
        gpus = sysinfo._parse_nvidia_smi_gpus(NVIDIA_SMI_GPUS)
        self.assertEqual(len(gpus), 2)
        self.assertEqual(gpus[0], {"index": 0, "name": "NVIDIA GeForce RTX 3090",
                                   "total_vram_bytes": 24576 * 1024 * 1024})
        self.assertEqual(gpus[1]["name"], "NVIDIA GeForce RTX 2060 SUPER")
        self.assertEqual(gpus[1]["total_vram_bytes"], 8192 * 1024 * 1024)

    def test_a_card_with_unreadable_size_keeps_its_name(self):
        """Knowing the card is there is still worth reporting -- a None size
        says "unknown", which is the honest answer, while dropping the row
        would say "no such GPU"."""
        gpus = sysinfo._parse_nvidia_smi_gpus("0, NVIDIA Whatever, [N/A]\n")
        self.assertEqual(len(gpus), 1)
        self.assertEqual(gpus[0]["name"], "NVIDIA Whatever")
        self.assertIsNone(gpus[0]["total_vram_bytes"])

    def test_gpu_listing_of_junk_is_empty(self):
        for text in ("", "garbage", "a, b, c", "0, only-two-fields"):
            with self.subTest(text=text):
                self.assertEqual(sysinfo._parse_nvidia_smi_gpus(text), [])


class TestGpuMemoryLookup(unittest.TestCase):
    """gpu_memory_bytes around the parser -- absent tool, absent driver data."""

    def _fake_smi(self, text):
        """Replace the subprocess layer so these run with no GPU present."""
        calls = []
        original = sysinfo._run_nvidia_smi
        self.addCleanup(setattr, sysinfo, "_run_nvidia_smi", original)

        def fake(args):
            calls.append(args)
            return text

        sysinfo._run_nvidia_smi = fake
        return calls

    def test_missing_nvidia_smi_returns_an_empty_dict(self):
        """An AMD machine, or any machine with no NVIDIA driver, must get {}
        back rather than an exception -- measurement never breaks a render."""
        original = sysinfo.find_nvidia_smi
        self.addCleanup(setattr, sysinfo, "find_nvidia_smi", original)
        sysinfo.find_nvidia_smi = lambda: ""
        self.assertEqual(sysinfo.gpu_memory_bytes([123, 456]), {})

    def test_no_pids_asked_about_skips_the_subprocess_entirely(self):
        """Shelling out to a driver tool to answer a question about nothing is
        pure cost on a path that may be sampled every second."""
        calls = self._fake_smi(NVIDIA_SMI_WITH_NUMBERS)
        self.assertEqual(sysinfo.gpu_memory_bytes([]), {})
        self.assertEqual(calls, [])

    def test_only_the_requested_pids_come_back(self):
        """nvidia-smi reports every process on the box; a caller asking about
        its own render must not be handed the browser's VRAM."""
        self._fake_smi(NVIDIA_SMI_WITH_NUMBERS)
        self.assertEqual(sysinfo.gpu_memory_bytes([4243]),
                         {4243: 512 * 1024 * 1024})

    def test_a_pid_holding_no_gpu_memory_is_simply_absent(self):
        """Absent, not zero -- see _parse_nvidia_smi's [N/A] test."""
        self._fake_smi(NVIDIA_SMI_WITH_NUMBERS)
        self.assertEqual(sysinfo.gpu_memory_bytes([999999]), {})

    def test_a_driver_that_reports_nothing_useful_returns_empty(self):
        """The real Windows consumer-card case, end to end."""
        self._fake_smi(NVIDIA_SMI_ALL_NA)
        self.assertEqual(sysinfo.gpu_memory_bytes([2284, 9268]), {})

    def test_a_failed_nvidia_smi_call_returns_empty(self):
        self._fake_smi("")
        self.assertEqual(sysinfo.gpu_memory_bytes([1, 2]), {})

    def test_unusable_pid_arguments_return_empty_rather_than_raising(self):
        """Callers pass whatever a Task carries. A None pid from a process
        that never started must not take the render down with it."""
        self._fake_smi(NVIDIA_SMI_WITH_NUMBERS)
        for pids in (None, 5, ["not-a-pid"], [None]):
            with self.subTest(pids=pids):
                self.assertEqual(sysinfo.gpu_memory_bytes(pids), {})

    def test_running_the_tool_never_raises_when_the_path_is_wrong(self):
        """A stale cached path, a driver uninstalled mid-session: "" not a
        FileNotFoundError."""
        original = sysinfo._nvidia_smi_path
        self.addCleanup(setattr, sysinfo, "_nvidia_smi_path", original)
        sysinfo._nvidia_smi_path = os.path.join(
            tempfile.gettempdir(), "definitely-not-nvidia-smi.exe")
        self.assertEqual(sysinfo._run_nvidia_smi(["--help"]), "")


class TestSysinfoProbes(unittest.TestCase):
    """The live probes, asserted only on their contract.

    Deliberately no assertion about how much RAM or which GPU this machine
    has -- these must pass on a laptop, a farm blade and a CI container alike.
    What is worth locking in is that every one of them returns a usable value
    or an honest None, and that none of them raises.
    """

    def test_total_ram_is_a_positive_int_or_none(self):
        total = sysinfo.total_ram_bytes()
        if total is not None:
            self.assertIsInstance(total, int)
            self.assertGreater(total, 0)

    def test_available_ram_is_a_non_negative_int_or_none(self):
        available = sysinfo.available_ram_bytes()
        if available is not None:
            self.assertIsInstance(available, int)
            self.assertGreaterEqual(available, 0)

    def test_available_never_exceeds_total(self):
        """A sanity check on the two readings being the same quantity. If a
        platform branch ever mixed up bytes and kB, this is where it shows."""
        total = sysinfo.total_ram_bytes()
        available = sysinfo.available_ram_bytes()
        if total is not None and available is not None:
            self.assertLessEqual(available, total)

    def test_current_rss_of_this_process_is_an_int_or_none(self):
        rss = sysinfo.current_rss(os.getpid())
        if rss is not None:
            self.assertIsInstance(rss, int)
            self.assertGreater(rss, 0)

    def test_current_rss_of_an_impossible_pid_is_none(self):
        """Never 0. A caller sampling a curve must be able to tell "the
        process is gone" from "the process is using no memory"."""
        for pid in (0, -1, None, "nope", 3.7):
            with self.subTest(pid=pid):
                self.assertIsNone(sysinfo.current_rss(pid))

    def test_peak_of_a_finished_child_is_an_int_or_none(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        peak = sysinfo.peak_working_set(proc)
        if peak is not None:
            self.assertIsInstance(peak, int)
            self.assertGreater(peak, 0)

    def test_peak_of_a_non_process_is_none(self):
        """Never 0 for something that was never measured."""
        self.assertIsNone(sysinfo.peak_working_set(None))
        self.assertIsNone(sysinfo.peak_working_set(object()))

    @unittest.skipUnless(sys.platform == "win32",
                         "PeakWorkingSetSize is a Windows-only guarantee")
    def test_peak_survives_the_process_exiting_on_windows(self):
        """The finding this module is built around: Windows keeps the process
        object alive while Popen holds a handle, so the peak can be read after
        wait() -- which is when it is final. If this ever regresses, callers
        would have to sample during the render instead, so it is worth a test
        rather than a comment.

        On Linux the opposite is true (VmHWM vanishes with the process), which
        is why this is guarded rather than asserted everywhere.
        """
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        after = sysinfo.peak_working_set(proc)
        self.assertIsNotNone(after)
        self.assertGreater(after, 0)
        # Still stable on a second read, and unchanged -- it is a high-water
        # mark, not a live figure.
        self.assertEqual(sysinfo.peak_working_set(proc), after)

    @unittest.skipUnless(sys.platform == "win32", "Windows handle semantics")
    def test_peak_is_none_once_the_handle_is_closed(self):
        """Documented failure mode: the reading depends on the handle, so a
        caller that closes it first gets an honest None, not a stale number."""
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        proc._handle.Close()
        self.assertIsNone(sysinfo.peak_working_set(proc))

    def test_find_nvidia_smi_returns_a_string_and_caches_it(self):
        """Never None -- callers test it with `if not path`. Cached because a
        machine with no NVIDIA card should not pay a filesystem walk on every
        sample of a render."""
        first = sysinfo.find_nvidia_smi()
        self.assertIsInstance(first, str)
        self.assertEqual(sysinfo.find_nvidia_smi(), first)
        if first:
            self.assertTrue(os.path.isfile(first))

    def test_gpu_memory_bytes_on_this_machine_does_not_raise(self):
        """Whatever this machine has -- an NVIDIA card, an AMD one, none at
        all -- asking must produce a dict and never an exception."""
        result = sysinfo.gpu_memory_bytes([os.getpid()])
        self.assertIsInstance(result, dict)
        for pid, used in result.items():
            self.assertIsInstance(pid, int)
            self.assertIsInstance(used, int)


class TestSysinfoDescribe(unittest.TestCase):
    """describe() is what the CLI prints so a user can tell "no GPU" from "no
    tool". Its keys are therefore a contract, not an implementation detail."""

    @classmethod
    def setUpClass(cls):
        # Once for the class: describe() shells out to nvidia-smi twice, and
        # doing that per test method would make the suite pay for it eight
        # times over on a machine that has a driver.
        cls.info = sysinfo.describe()

    def test_every_documented_key_is_present(self):
        for key in ("platform", "platform_detail", "total_ram_bytes",
                    "available_ram_bytes", "peak_rss_available",
                    "peak_rss_method", "nvidia_smi", "gpus",
                    "per_process_vram", "notes"):
            with self.subTest(key=key):
                self.assertIn(key, self.info)

    def test_platform_and_method_are_strings(self):
        self.assertIsInstance(self.info["platform"], str)
        self.assertIsInstance(self.info["platform_detail"], str)
        self.assertIsInstance(self.info["peak_rss_method"], str)
        self.assertIsInstance(self.info["nvidia_smi"], str)

    def test_capability_flags_are_real_booleans(self):
        """These drive `if` branches in the CLI's report; a truthy string or a
        None would read as a capability the machine may not have."""
        self.assertIsInstance(self.info["peak_rss_available"], bool)
        self.assertIsInstance(self.info["per_process_vram"], bool)

    def test_ram_values_are_ints_or_none(self):
        for key in ("total_ram_bytes", "available_ram_bytes"):
            with self.subTest(key=key):
                value = self.info[key]
                self.assertTrue(value is None or isinstance(value, int))

    def test_gpu_entries_have_the_documented_shape(self):
        self.assertIsInstance(self.info["gpus"], list)
        for gpu in self.info["gpus"]:
            self.assertIn("index", gpu)
            self.assertIsInstance(gpu["name"], str)
            total = gpu["total_vram_bytes"]
            self.assertTrue(total is None or isinstance(total, int))

    def test_a_missing_tool_is_explained_rather_than_left_blank(self):
        """The reason describe() exists. If nvidia-smi is absent the report
        must say so in words, so "VRAM unknown" is not mistaken for
        "this render used no VRAM"."""
        if not self.info["nvidia_smi"]:
            self.assertEqual(self.info["gpus"], [])
            self.assertFalse(self.info["per_process_vram"])
            self.assertTrue(any("nvidia-smi" in n for n in self.info["notes"]))

    def test_notes_are_plain_strings(self):
        self.assertIsInstance(self.info["notes"], list)
        for note in self.info["notes"]:
            self.assertIsInstance(note, str)
            self.assertTrue(note.strip())

    def test_describe_is_json_serialisable(self):
        """It ends up in CLI output and may end up in a farm submission or a
        bug report; a ctypes object leaking into it would only fail there."""
        json.dumps(self.info)

    def test_it_says_whether_a_wrapper_script_would_be_seen_through(self):
        """The single-process/whole-tree difference is a factor of forty on a
        launcher script, and nothing else in the report reveals which one a
        number is. So it is a documented key, not an inference."""
        self.assertIsInstance(self.info["peak_rss_tree"], bool)
        self.assertIsInstance(self.info["peak_rss_tree_method"], str)
        # A capability claimed must name its mechanism, so a wrong figure can
        # be traced to the thing that produced it.
        if self.info["peak_rss_tree"]:
            self.assertTrue(self.info["peak_rss_tree_method"].strip())
        else:
            self.assertEqual(self.info["peak_rss_tree_method"], "")

    def test_the_children_gap_is_described_the_way_it_actually_is(self):
        """describe() used to state flatly that children are never covered.
        Once they are, saying so anyway would be its own quiet lie -- the
        notes have to track the machine, not the old limitation."""
        notes = " ".join(self.info["notes"]).lower()
        if self.info["peak_rss_tree"]:
            self.assertIn("whole process tree", notes)
        elif sys.platform == "win32":
            self.assertIn("not", notes)
            self.assertIn("children", notes)


class TestTreeMemoryMeasurement(unittest.TestCase):
    """Whole-process-tree peak memory, via a Windows Job Object.

    The defect these exist for: ``peak_working_set`` covers only the process
    hsl spawned, so a studio ``husk.bat`` wrapper -- which ``RenderJob.
    husk_exe`` may legitimately point at -- measured 8 MB for a render that
    really used 300 MB. A silently *small* number is the worst possible
    outcome: preflight compares it against installed RAM and cheerfully
    reports plenty of room for a shot that will swap for six hours.

    Job Objects are Windows-only, so the behavioural cases are guarded; the
    contract cases (None in, None out, never raises) must hold everywhere.
    """

    def _child(self, tmp, payload_mb=0, linger=0.25):
        """A script that allocates and *touches* payload_mb, then lingers.

        Touching matters -- an untouched bytearray is committed but never
        faulted in, and half the point is measuring what really landed in
        memory. The linger removes a race that has nothing to do with what is
        being tested: assignment happens just after the spawn, so a child that
        exits instantly could be gone before it is adopted.
        """
        path = os.path.join(tmp, "child_%d.py" % payload_mb)
        with open(path, "w") as fh:
            fh.write("import time\n")
            if payload_mb:
                fh.write("b = bytearray(%d * 1024 * 1024)\n"
                         "for i in range(0, len(b), 4096): b[i] = 1\n"
                         % payload_mb)
            fh.write("time.sleep(%r)\n" % linger)
        return path

    def _measure(self, cmd):
        """Spawn cmd the way runner.py does and report (single, tree)."""
        job = sysinfo.open_job_object()
        self.addCleanup(sysinfo.close_job_object, job)
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        assigned = sysinfo.assign_process_to_job(job, proc)
        proc.wait()
        return assigned, sysinfo.peak_working_set(proc), sysinfo.peak_job_memory(job)

    # -- contract, everywhere ---------------------------------------------

    def test_every_call_tolerates_having_nothing_to_measure(self):
        """A measurement must never be able to take a render down with it, so
        there is no argument any of these will raise on."""
        self.assertFalse(sysinfo.assign_process_to_job(None, None))
        self.assertFalse(sysinfo.assign_process_to_job(None, object()))
        self.assertIsNone(sysinfo.peak_job_memory(None))
        self.assertIsNone(sysinfo.close_job_object(None))

    def test_support_is_a_cached_boolean(self):
        """Callers branch on it, so a truthy string would read as a capability
        the machine may not have. Cached because the probe creates a real
        kernel object and a render should not pay for it repeatedly."""
        first = sysinfo.supports_tree_measurement()
        self.assertIsInstance(first, bool)
        self.assertIs(sysinfo.supports_tree_measurement(), first)

    @unittest.skipIf(sys.platform == "win32", "the non-Windows answer")
    def test_a_platform_without_job_objects_admits_it(self):
        """Unknown, stated. Silently handing back a single-process figure
        under a whole-tree name is the failure being avoided."""
        self.assertFalse(sysinfo.supports_tree_measurement())
        self.assertIsNone(sysinfo.open_job_object())
        self.assertFalse(sysinfo.assign_process_to_job(None, None))

    # -- behaviour, Windows ------------------------------------------------

    @unittest.skipUnless(sys.platform == "win32", "Job Objects are Windows-only")
    def test_a_job_nothing_ran_in_reports_none_not_zero(self):
        """The kernel really does answer 0 for an empty job. Passing that on
        would claim a render used no memory, which is never true -- the same
        rule the rest of sysinfo is built around."""
        job = sysinfo.open_job_object()
        self.addCleanup(sysinfo.close_job_object, job)
        self.assertIsNotNone(job)
        self.assertIsNone(sysinfo.peak_job_memory(job))

    @unittest.skipUnless(sys.platform == "win32", "Windows handle semantics")
    def test_closing_a_job_twice_is_harmless(self):
        """A cancelled render can unwind through the same finally twice, and
        double-closing a Windows handle is how an unrelated handle that has
        since reused the value gets corrupted."""
        job = sysinfo.open_job_object()
        sysinfo.close_job_object(job)
        sysinfo.close_job_object(job)
        self.assertIsNone(sysinfo.peak_job_memory(job))

    @unittest.skipUnless(sys.platform == "win32", "Job Objects are Windows-only")
    def test_a_wrapper_script_is_measured_through_rather_than_instead_of(self):
        """The defect itself, in one test.

        A .bat that launches a Python child which allocates and touches a real
        payload. The wrapper's own footprint is a few MB; the tree's is the
        payload. Measured on this machine at 300 MB: 8.0 MB single-process
        against 315.6 MB whole-tree. The payload here is smaller only so the
        suite stays cheap on a modest box -- the gap is the point, not the
        absolute number.
        """
        payload_mb = 150
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        child = self._child(tmp, payload_mb)
        wrapper = os.path.join(tmp, "wrap.bat")
        with open(wrapper, "w") as fh:
            fh.write('@echo off\r\n"%s" "%s"\r\n' % (sys.executable, child))

        assigned, single, tree = self._measure([wrapper])
        self.assertTrue(assigned)
        # The wrapper alone is tiny. That reading is not wrong, it is just an
        # answer to a different question -- and it is why it must be labelled.
        self.assertIsNotNone(single)
        self.assertLess(single, 50 * 1024 ** 2)
        # The tree it started really did allocate the payload.
        self.assertIsNotNone(tree)
        self.assertGreater(tree, payload_mb * 1024 ** 2)

    @unittest.skipUnless(sys.platform == "win32", "Job Objects are Windows-only")
    def test_a_spawn_with_no_wrapper_of_our_own_is_measured_correctly(self):
        """The case that already worked must not regress: no .bat in the way,
        and the payload still has to show up.

        Only the tree figure is asserted, and that is not laziness --
        ``sys.executable`` is not necessarily one process. In this repo's venv
        it is a 45 KB uv trampoline that spawns the real interpreter as a
        child, so even this "direct" spawn is a two-process tree and its
        single-process peak reads about 5 MB against a 150 MB payload. That is
        the same defect as the .bat wrapper, arriving with no .bat involved,
        and it is why the whole-tree figure is the one worth pinning."""
        payload_mb = 150
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        assigned, single, tree = self._measure(
            [sys.executable, self._child(tmp, payload_mb)])
        self.assertTrue(assigned)
        self.assertIsNotNone(single)                    # still a real reading
        self.assertIsNotNone(tree)
        self.assertGreater(tree, payload_mb * 1024 ** 2)

    @unittest.skipUnless(sys.platform == "win32", "Job Objects are Windows-only")
    def test_a_genuinely_single_process_spawn_agrees_both_ways(self):
        """Where the interpreter really is one process -- ``sys._base_executable``
        under a venv -- the two mechanisms must land on the same story, or the
        job figure is measuring something other than what it claims. They will
        not be equal: the job counts committed memory and the working set
        counts resident pages. Both must see the payload.

        Skipped rather than faked where the base interpreter cannot be found,
        because a test that quietly measures the trampoline again would prove
        nothing."""
        base = getattr(sys, "_base_executable", None) or sys.executable
        if not base or not os.path.isfile(base):
            self.skipTest("no base interpreter to spawn directly")
        payload_mb = 150
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        assigned, single, tree = self._measure(
            [base, self._child(tmp, payload_mb)])
        self.assertTrue(assigned)
        if single is None or single < payload_mb * 1024 ** 2:
            self.skipTest("%s is itself a launcher, not a single process" % base)
        self.assertGreater(tree, payload_mb * 1024 ** 2)

    @unittest.skipUnless(sys.platform == "win32", "Job Objects are Windows-only")
    def test_a_trivial_process_still_reports_a_small_figure(self):
        """The other direction of wrong. If the job ever measured something
        wider than the tree it owns -- this test process, or the machine -- a
        child that does nothing would come back large, and every render would
        look like it needed gigabytes it never touched."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        assigned, _, tree = self._measure([sys.executable, self._child(tmp)])
        self.assertTrue(assigned)
        self.assertIsNotNone(tree)
        self.assertLess(tree, 100 * 1024 ** 2)

    @unittest.skipUnless(sys.platform == "win32", "Job Objects are Windows-only")
    def test_a_process_already_in_a_job_is_handled_either_way(self):
        """Nested jobs are a Windows 8+ feature, and hsl is often not the only
        thing making them -- CI runners and some terminals put everything they
        start inside one. Whether a *second* assignment succeeds is therefore
        a property of the machine, not of this code: both answers are fine,
        raising is not, and the first job must keep its measurement regardless.

        Measured here: it succeeds. This repo's own test process runs inside a
        job already, so the ordinary path is the nested one.
        """
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        outer = sysinfo.open_job_object()
        inner = sysinfo.open_job_object()
        self.addCleanup(sysinfo.close_job_object, outer)
        self.addCleanup(sysinfo.close_job_object, inner)

        proc = subprocess.Popen([sys.executable, self._child(tmp)],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        first = sysinfo.assign_process_to_job(outer, proc)
        second = sysinfo.assign_process_to_job(inner, proc)
        proc.wait()

        self.assertTrue(first)
        self.assertIsInstance(second, bool)
        self.assertIsNotNone(sysinfo.peak_job_memory(outer))


class TestTreeMemoryInTheQueue(unittest.TestCase):
    """RenderQueue has to open the job before it spawns, because a Job Object
    cannot adopt a tree retroactively. These pin that ordering and the
    fallback, with the platform calls stubbed so they run anywhere."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        script = os.path.join(self.dir, "fake_husk.py")
        with open(script, "w") as fh:
            fh.write("print('ALF_PROGRESS 100%', flush=True)\n")
        if os.name == "nt":
            self.exe = os.path.join(self.dir, "fake_husk.bat")
            with open(self.exe, "w") as fh:
                fh.write(f'@echo off\n"{sys.executable}" "{script}" %*\n')
        else:
            self.exe = script
            os.chmod(script, os.stat(script).st_mode | stat.S_IEXEC)

        # Restore every platform call this class stubs, whichever it touched.
        for name in ("open_job_object", "assign_process_to_job",
                     "peak_job_memory", "close_job_object", "peak_working_set"):
            self.addCleanup(setattr, sysinfo, name, getattr(sysinfo, name))

    def _job(self):
        return RenderJob(usd_file=os.path.join(self.dir, "shot.usd"),
                         engine="husk", husk_exe=self.exe,
                         hip_file=os.path.join(self.dir, "shot.hip"),
                         rop_path="/stage/rop1", chunk=FrameChunk(1, 1, 1))

    def _run(self, **kwargs):
        queue = RenderQueue([self._job()], record_memory=False, **kwargs)
        queue.start(block=True)
        return queue, queue.tasks[0]

    def test_a_whole_tree_figure_beats_the_single_process_one(self):
        # Both are available and they disagree by a factor of forty -- which
        # is exactly the wrapper-script case. The tree figure is the render.
        sysinfo.peak_job_memory = lambda job: 300 * 1024 ** 2
        sysinfo.peak_working_set = lambda proc: 8 * 1024 ** 2
        _, task = self._run()
        self.assertEqual(task.peak_rss, 300 * 1024 ** 2)
        self.assertTrue(task.peak_rss_is_tree)

    def test_it_falls_back_to_the_single_process_peak_and_says_so(self):
        # No job to be had (any non-Windows machine, or a Windows that refused
        # one). The old figure is still worth having -- but a consumer must be
        # able to tell it apart from a whole-tree one, or it will size a farm
        # off a launcher's footprint.
        sysinfo.peak_job_memory = lambda job: None
        sysinfo.peak_working_set = lambda proc: 8 * 1024 ** 2
        _, task = self._run()
        self.assertEqual(task.peak_rss, 8 * 1024 ** 2)
        self.assertFalse(task.peak_rss_is_tree)

    def test_the_job_is_opened_before_the_spawn_and_closed_after(self):
        """The ordering constraint the whole mechanism rests on: a Job Object
        must exist *before* CreateProcess, because AssignProcessToJobObject
        cannot adopt children the target has already made. Opening it after
        the Popen would compile, run, and quietly measure the wrapper again.

        Checked by watching the task's own process handle: None when the job
        is opened, present by the time the child is assigned."""
        token = object()
        seen = {}
        calls = []
        queue = RenderQueue([self._job()], record_memory=False)
        task = queue.tasks[0]

        def opened():
            calls.append("open")
            seen["proc_at_open"] = task._proc
            return token

        def assign(job, proc):
            calls.append("assign")
            seen["job"] = job
            seen["proc_at_assign"] = task._proc
            return True

        sysinfo.open_job_object = opened
        sysinfo.assign_process_to_job = assign
        sysinfo.peak_job_memory = lambda job: calls.append("peak")
        sysinfo.close_job_object = lambda job: calls.append("close")

        queue.start(block=True)
        self.assertEqual(calls, ["open", "assign", "peak", "close"])
        self.assertIs(seen["job"], token)
        self.assertIsNone(seen["proc_at_open"])         # before the spawn
        self.assertIsNotNone(seen["proc_at_assign"])    # adopted right after

    def test_no_job_is_opened_when_measurement_is_switched_off(self):
        # measure_memory=False means no instrumentation at all, not cheaper
        # instrumentation -- a caller that turned it off should pay nothing.
        calls = []
        sysinfo.open_job_object = lambda: calls.append("open")
        _, task = self._run(measure_memory=False)
        self.assertEqual(calls, [])
        self.assertIsNone(task.peak_rss)
        self.assertFalse(task.peak_rss_is_tree)

    def test_a_measurement_that_explodes_never_breaks_the_render(self):
        # Every one of these is a ctypes call into the OS. If measuring can
        # fail a render, measuring is worse than not measuring at all.
        def boom(*args, **kwargs):
            raise RuntimeError("the OS said no")

        for name in ("open_job_object", "assign_process_to_job",
                     "peak_job_memory", "close_job_object", "peak_working_set"):
            setattr(sysinfo, name, boom)
        queue, task = self._run()
        self.assertEqual(task.state, State.DONE)
        self.assertIsNone(task.peak_rss)
        self.assertEqual(queue.warnings, [])

    def test_the_filed_sample_records_which_kind_of_figure_it_was(self):
        """A history that mixes whole-tree and single-process numbers without
        labelling them cannot be read at all -- preflight would compare a
        wrapper's 8 MB against installed RAM as if it were the render."""
        filed = []
        original = memlog.record
        memlog.record = filed.append
        self.addCleanup(setattr, memlog, "record", original)

        sysinfo.peak_job_memory = lambda job: 300 * 1024 ** 2
        queue = RenderQueue([self._job()])          # record_memory left on
        queue.start(block=True)
        self.assertEqual(len(filed), 1)
        self.assertEqual(filed[0].peak_rss, 300 * 1024 ** 2)
        self.assertTrue(filed[0].peak_rss_is_tree)


class TestTreeMemoryReporting(unittest.TestCase):
    """`hsl memory` must never present a single-process figure as if it were
    the whole tree. Its own store, so the assertions do not depend on what
    other tests in this file happened to record."""

    def setUp(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        original = memlog.LOG_FILE
        memlog.LOG_FILE = os.path.join(tmpdir.name, "memory.json")
        self.addCleanup(setattr, memlog, "LOG_FILE", original)

    def _run(self, argv):
        args = build_parser().parse_args(argv)
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()):
            code = cli_mod.cmd_memory(args)
        return code, out.getvalue()

    def _record(self, is_tree):
        memlog.record(memlog.MemorySample(
            hip_path="/jobs/shot.hip", rop_path="/stage/rop1", engine="husk",
            frames=1, peak_rss=8 * 1024 ** 2, peak_rss_is_tree=is_tree,
            when=time.time()))

    def test_a_single_process_figure_is_labelled_and_explained(self):
        # 8 MB for a render is only believable if you do not know it was the
        # wrapper that got measured. The report has to say which it was.
        self._record(is_tree=False)
        code, out = self._run(["memory"])
        self.assertEqual(code, 0)
        self.assertIn("main process only", out)
        self.assertNotIn("whole tree", out)
        self.assertIn("wrapper script", out)

    def test_a_whole_tree_figure_says_that_instead(self):
        self._record(is_tree=True)
        code, out = self._run(["memory"])
        self.assertEqual(code, 0)
        self.assertIn("whole tree", out)
        self.assertNotIn("main process only", out)

    def test_the_worst_peak_line_carries_the_scope_too(self):
        """It is the line someone quotes when sizing a machine, so it is the
        last place a single-process figure should pass as a total."""
        self._record(is_tree=False)
        original = sysinfo.total_ram_bytes
        sysinfo.total_ram_bytes = lambda: 64 * 1024 ** 3
        self.addCleanup(setattr, sysinfo, "total_ram_bytes", original)
        _, out = self._run(["memory"])
        self.assertIn("Worst recorded peak", out)
        self.assertIn("main process only", out.split("Worst recorded peak")[1])

    def test_the_machine_report_states_whether_tree_measurement_works(self):
        # So someone reading a suspiciously small number can find out whether
        # this machine was even capable of the right one.
        code, out = self._run(["memory", "--machine"])
        self.assertEqual(code, 0)
        self.assertIn("whole-tree RAM", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
