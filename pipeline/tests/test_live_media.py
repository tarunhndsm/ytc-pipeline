from __future__ import annotations

import io
import json
import shutil
import subprocess
import tarfile
import tempfile
import time
import unittest
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from ytc.news import live_export
from ytc import ff
from ytc.news.live import media, news, package, policy, seo, visuals
from ytc.news.live.voice import Voice, key_of


class LiveMediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = datetime.now(timezone.utc)
        self.photo = self.root / "source.jpg"
        Image.new("RGB", (900, 300), "blue").save(self.photo)
        self.wav = self.root / "voice.wav"
        with wave.open(str(self.wav), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000)
            w.writeframes(b"\0\0" * 96000)
        self.script = "नगर निगम ने सोमवार से नई बस सेवा शुरू की। इस रूट पर पहले केवल निजी बसें चलती थीं।"
        self.result = {"package": {"verified": True, "publish": True, "headline": "नई बस सेवा शुरू",
                                  "script": self.script, "context": "इस रूट पर पहले केवल निजी बसें चलती थीं।",
                                  "title": "City Bus: नई सेवा शुरू", "label": "देश", "format": "explainer",
                                  "source_links": [{"outlet": "Council", "url": "https://example.org/report"}]},
                       "live_assets": {"photos": [{"path": str(self.photo), "title": "File:Bus.jpg",
                                                   "credit": "Reporter / CC BY 4.0", "licence": "CC BY 4.0",
                                                   "artist": "Reporter", "source_url": "https://example.org/Bus.jpg"}],
                                       "clips": [], "voice": str(self.wav)}}
        self.made = {"id": "20261101-bus", "at": self.now.isoformat(), "topic": "bus", "breaking": True,
                     "publish": {"youtube_id": "abcdefghijk", "status": "sent"}}

    def tearDown(self):
        self.tmp.cleanup()

    def packed(self):
        return live_export.bundle(self.result, self.made, self.root / "live.tar.gz")

    def test_real_export_import_reuses_exact_original_script_audio_and_licence(self):
        packed = self.packed()
        with patch.object(media, "INBOX", self.root / "inbox"):
            story = media.install(packed, "123", self.now)
        self.assertEqual(story["script"], self.script)
        self.assertEqual(story["at"], self.made["at"])
        self.assertEqual(story["media"][0]["licence"], "CC BY 4.0")
        self.assertEqual(story["youtube_id"], "abcdefghijk")
        voice = object.__new__(Voice)
        voice.language, voice.cache = "hi-IN", self.root / "voice_local"
        voice.clips, voice.adopted = {}, {}
        voice.cache.mkdir()
        self.assertTrue(voice.adopt(self.script, story["narration_path"]))
        adopted = voice.cache / ("say-" + key_of(self.script) + ".wav")
        with wave.open(str(adopted)) as w:
            self.assertEqual(w.getframerate(), 48000)
            self.assertEqual(w.getnframes(), 96000)
        voice.language = "en-IN"
        self.assertFalse(voice.adopt(self.script, story["narration_path"]))

    def test_production_narration_replaces_a_cached_local_read_and_reloads_in_memory(self):
        voice = object.__new__(Voice)
        voice.language, voice.cache = "hi-IN", self.root / "voice_local"
        voice.cache.mkdir()
        key = "say-" + key_of(self.script)
        target = voice.cache / f"{key}.wav"
        target.write_bytes(b"old locally generated read")
        voice.clips, voice.adopted = {key: object()}, {}
        self.assertTrue(voice.adopt(self.script, str(self.wav)))
        self.assertEqual(target.read_bytes(), self.wav.read_bytes())
        self.assertNotIn(key, voice.clips)
        loaded = object()
        voice.clips[key] = loaded
        self.assertTrue(voice.adopt(self.script, str(self.wav)))
        self.assertIs(voice.clips[key], loaded)  # a repeated index poll does not reload unchanged audio

    def test_invalid_production_narration_keeps_the_current_read(self):
        voice = object.__new__(Voice)
        voice.language, voice.cache = "hi-IN", self.root / "voice_local"
        voice.cache.mkdir()
        key = "say-" + key_of(self.script)
        target = voice.cache / f"{key}.wav"
        target.write_bytes(self.wav.read_bytes())
        existing = object()
        voice.clips, voice.adopted = {key: existing}, {}
        invalid = self.root / "empty.wav"
        with wave.open(str(invalid), "wb") as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(48000)
        self.assertFalse(voice.adopt(self.script, str(invalid)))
        self.assertEqual(target.read_bytes(), self.wav.read_bytes())
        self.assertIs(voice.clips[key], existing)

    def test_download_does_not_make_stale_or_future_reporting_fresh(self):
        for delta in (timedelta(hours=-25), timedelta(hours=1)):
            self.made["at"] = (self.now + delta).isoformat()
            with patch.object(media, "INBOX", self.root / "inbox"):
                self.assertIsNone(media.install(self.packed(), "123", self.now))

    def test_unverified_copy_and_restricted_media_are_never_exported(self):
        for field, value in (("verified", "true"), ("publish", False)):
            self.result["package"][field] = value
            with self.assertRaises(ValueError):
                self.packed()
            self.result["package"][field] = True
        self.result["live_assets"]["photos"][0]["licence"] = "CC BY-NC 4.0"
        with self.assertRaises(ValueError):
            self.packed()

    def test_media_merge_deduplicates_social_upload_and_preserves_english_language(self):
        with patch.object(media, "INBOX", self.root / "inbox"):
            report = media.install(self.packed(), "123", self.now)
        index = self.root / "index.json"
        index.write_text(json.dumps({"stories": [report]}))
        old = {"id": "yt-abcdefghijk", "youtube_id": "abcdefghijk", "headline": "Social headline",
               "script": "An English news report.", "angle": "English context.", "at": self.made["at"], "views": 51}
        hi = media.merge([old], index=index)
        self.assertEqual(len(hi), 1)
        self.assertEqual(hi[0]["script"], self.script)
        self.assertEqual(hi[0]["id"], old["id"])
        self.assertEqual(hi[0]["views"], 51)
        en = media.merge([old], english=True, index=index)
        self.assertEqual(en[0]["script"], old["script"])
        self.assertNotIn("narration_path", en[0])
        self.assertTrue(en[0]["media"])
        self.assertEqual(media.merge([], english=True, index=index), [])

    def test_waiting_buffer_post_matches_later_upload_by_exact_title(self):
        report = {**self.result["package"], "id": self.made["id"], "at": self.made["at"], "media": [],
                  "youtube_id": "", "media_revision": "123"}
        index = self.root / "index.json"
        index.write_text(json.dumps({"stories": [report]}))
        old = {"id": "yt-abcdefghijk", "title": report["title"] + " #Bus", "youtube_id": "abcdefghijk"}
        out = media.merge([old], index=index)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["youtube_id"], "abcdefghijk")

    def test_backfilled_reference_visual_never_changes_or_certifies_existing_reporting(self):
        report = {"id": "seed", "headline": "Reference association", "youtube_id": "abcdefghijk",
                  "at": self.made["at"], "visuals_only": True, "media": [{"path": "reference.jpg"}],
                  "media_revision": "seed"}
        index = self.root / "index.json"
        index.write_text(json.dumps({"stories": [report]}))
        old = {"id": "yt-abcdefghijk", "youtube_id": "abcdefghijk", "headline": "Original headline",
               "angle": "Original summary", "at": self.made["at"]}
        merged = media.merge([old], index=index)
        self.assertEqual(merged[0]["headline"], old["headline"])
        self.assertNotIn("verified", merged[0])
        self.assertNotIn("script", merged[0])
        self.assertTrue(merged[0]["media"])
        self.assertEqual(media.merge([], index=index), [])

    def test_receiver_fetches_once_and_rejects_expired_reporting_without_redating(self):
        packed = self.packed()
        class Store:
            def __init__(self): self.downloads = 0
            def assets(inner, release):
                self.assertEqual(release, package.RELEASE)
                return {"live-one.tar.gz": {"id": 123, "name": "live-one.tar.gz", "size": packed.stat().st_size,
                                            "created_at": self.now.isoformat()}}
            def get(inner, release, name, target):
                inner.downloads += 1
                target.write_bytes(packed.read_bytes())
                return True
        store = Store()
        with patch.object(media, "INBOX", self.root / "inbox"):
            self.assertEqual(media.sync(store, self.now), 1)
            self.assertEqual(media.sync(store, self.now), 0)
            self.assertEqual(store.downloads, 1)
            self.assertEqual(media.sync(store, self.now + timedelta(hours=25)), 0)
            data = json.loads((media.INBOX / "live_media.json").read_text())
            self.assertEqual(data["stories"], [])

    def test_media_revision_refreshes_scene_without_view_count_churn(self):
        story = {"id": "x", "headline": "Headline", "media_revision": "one"}
        self.assertNotEqual(policy.revision(story), policy.revision({**story, "media_revision": "two"}))
        self.assertEqual(policy.revision(story), policy.revision({**story, "views": 1000}))

    def test_current_wire_keeps_news_cards_on_air_when_the_owned_story_queue_is_empty(self):
        prog = object.__new__(news.News)
        prog.audience = SimpleNamespace(shouts=[], waiting_hello=[])
        prog.scene, prog.stage, prog.since_cta = None, "count", 0
        prog.fresh, prog.stories, prog.cycle = [], [], []
        prog.ticker_stories = [{"headline": "A sourced current headline", "wire": True}]
        prog._load = lambda: None
        prog._take = lambda: None
        self.assertIsInstance(prog.next_scene(0), news.Waiting)

    def test_live_description_retains_visual_provenance_within_youtube_budget(self):
        story = {"id": "report", "at": self.made["at"], "headline": "Current reporting",
                 "media": self.result["live_assets"]["photos"]}
        text = seo.with_credits("बहुत लंबा विवरण " * 2000, [story])
        self.assertLessEqual(len(text.encode()), 4800)
        self.assertIn("https://example.org/Bus.jpg", text)
        self.assertIn("CC BY 4.0", text)
        self.assertIn("Reporter", text)

    def test_wide_reference_photo_keeps_both_edges_visible(self):
        image = Image.new("RGB", (900, 300), "blue")
        for x in range(80):
            for y in range(300):
                image.putpixel((x, y), (255, 0, 0))
                image.putpixel((899 - x, y), (0, 255, 0))
        image.save(self.photo)
        layer = visuals.photograph(str(self.photo), (852, 392))
        self.assertGreater(layer.rgb[196, 30, 0], 200)
        self.assertGreater(layer.rgb[196, 822, 1], 200)

    def test_unsafe_or_undeclared_tar_members_are_rejected(self):
        clean = self.packed()
        payload = {}
        with tarfile.open(clean, "r:gz") as tar:
            payload = {m.name: tar.extractfile(m).read() for m in tar.getmembers()}
        for name, kind in (("../escaped", tarfile.REGTYPE), ("/absolute", tarfile.REGTYPE),
                           ("alias.jpg", tarfile.SYMTYPE), ("undeclared.txt", tarfile.REGTYPE)):
            path = self.root / "unsafe.tar.gz"
            with tarfile.open(path, "w:gz") as tar:
                for key, value in payload.items():
                    member = tarfile.TarInfo(key); member.size = len(value)
                    tar.addfile(member, io.BytesIO(value))
                member = tarfile.TarInfo(name); member.type = kind; member.linkname = "photo0.jpg"
                tar.addfile(member, io.BytesIO())
            with self.assertRaises(ValueError):
                package.unpack(path, self.root / "extract")
        self.assertFalse((self.root / "escaped").exists())

    def test_tampered_media_digest_is_rejected(self):
        path = self.packed()
        with tarfile.open(path, "r:gz") as tar:
            payload = {m.name: tar.extractfile(m).read() for m in tar.getmembers()}
        payload["photo0.jpg"] = b"changed"
        with tarfile.open(path, "w:gz") as tar:
            for name, value in payload.items():
                member = tarfile.TarInfo(name); member.size = len(value)
                tar.addfile(member, io.BytesIO(value))
        with self.assertRaisesRegex(ValueError, "digest"):
            package.unpack(path, self.root / "extract")

    def test_validated_youtube_cc_footage_is_exported_with_standard_licence_and_no_audio(self):
        clip = self.root / "source.mp4"
        ff.run("-f", "lavfi", "-i", "color=c=red:s=320x180:d=1", "-c:v", "libx264", "-threads", "1", clip)
        self.result["live_assets"]["clips"] = [{"path": str(clip), "paths": {"live": str(clip)},
                                               "licence": "creativeCommon", "channel": "Owner newsroom",
                                               "credit": "Owner newsroom / YouTube (CC BY)",
                                               "source_url": "https://youtu.be/abcdefghijk", "checked_at": self.made["at"]}]
        data = package.unpack(self.packed(), self.root / "extracted")
        clip_meta = next(m for m in data["media"] if m["kind"] == "clip")
        self.assertEqual(clip_meta["licence"], "CC BY 3.0")
        video = self.root / "extracted" / clip_meta["file"]
        probe = subprocess.run([ff.exe(), "-i", str(video)], capture_output=True, text=True)
        self.assertIn("960x540", probe.stderr)
        self.assertNotIn("Audio:", probe.stderr)
        if shutil.which("ffmpeg"):
            decoder = visuals.Footage(str(video), 320, 180)
            try:
                deadline = time.monotonic() + 3
                frame = None
                while frame is None and time.monotonic() < deadline:
                    frame = decoder.frame(0)
                    time.sleep(0.02)
                self.assertIsNotNone(frame)
                self.assertEqual(frame.shape, (180, 320, 3))
                self.assertLessEqual(decoder.q.qsize(), 4)
            finally:
                decoder.close()
            self.assertIsNotNone(decoder.proc.poll())
            self.assertFalse(decoder.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
