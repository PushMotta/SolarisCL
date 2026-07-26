"""Tests for everything that does not need Houdini installed."""

import contextlib
import io
import os
import stat
import sys
import tempfile
import unittest

import shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hsl import bridge, farm, husk as husk_mod, preflight, presets
from hsl.bridge import (
    find_hython, list_hython_installations, load_user_settings, save_user_setting,
)
from hsl.cli import (
    _resolve_hython_choice, build_parser, cmd_render, parse_frames,
)
from hsl.husk import (
    DEFAULT_ENGINE, FrameChunk, RenderJob, build_command, expand_frame_token,
    format_command, frame_chunks, has_unexpanded_tokens, jobs_for_rop,
    looks_like_error, parse_progress,
)
from hsl.manifest import (
    Camera, LiveVolume, MissingAsset, RenderProduct, RenderRop, RenderSettings,
    RenderVar, SceneManifest,
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
