"""Synthetic API objects shaped like Instagram's real responses (no real users or links)."""

CDN = "https://cdn.example"
TAKEN_AT = 1700000000

DASH_WITH_AUDIO = f"""<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" minBufferTime="PT1.500S" type="static"
     mediaPresentationDuration="PT0H0M13.000S" profiles="urn:mpeg:dash:profile:isoff-on-demand:2011">
<Period duration="PT0H0M13.000S">
<AdaptationSet segmentAlignment="true" maxWidth="1080" maxHeight="1920" par="9:16" lang="und">
<Representation id="1v" mimeType="video/mp4" codecs="avc1.640028" width="1080" height="1920" frameRate="30"
  bandwidth="4000000" FBContentLength="6500000" FBQualityClass="hd" FBQualityLabel="1080p">
  <BaseURL>{CDN}/v1080.mp4?a=1&amp;b=2</BaseURL>
  <SegmentBase indexRangeExact="true" indexRange="845-900"><Initialization range="0-844"/></SegmentBase>
</Representation>
<Representation id="2v" mimeType="video/mp4" codecs="avc1.64001F" width="720" height="1280" frameRate="30"
  bandwidth="1800000" FBContentLength="2900000" FBQualityLabel="720p"><BaseURL>{CDN}/v720.mp4</BaseURL></Representation>
<Representation id="3v" mimeType="video/mp4" codecs="avc1.64001F" width="720" height="1280" frameRate="30"
  bandwidth="900000" FBContentLength="1450000" FBQualityLabel="540p"><BaseURL>{CDN}/v720low.mp4</BaseURL></Representation>
</AdaptationSet>
<AdaptationSet segmentAlignment="true" lang="und">
<Representation id="1a" bandwidth="131525" codecs="mp4a.40.2" mimeType="audio/mp4" audioSamplingRate="48000"
  FBContentLength="210000"><BaseURL>{CDN}/audio.mp4</BaseURL></Representation>
<Representation id="2a" bandwidth="64000" codecs="mp4a.40.5" mimeType="audio/mp4" audioSamplingRate="44100">
  <BaseURL>{CDN}/audio_low.mp4</BaseURL></Representation>
</AdaptationSet>
</Period>
</MPD>"""

DASH_VIDEO_ONLY = f"""<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"><Period>
<AdaptationSet><Representation id="1v" mimeType="video/mp4" codecs="avc1.64001F" width="720" height="1280"
  bandwidth="3140618"><BaseURL>{CDN}/silent720.mp4</BaseURL></Representation></AdaptationSet>
</Period></MPD>"""


def photo_node(pk="3001"):
    sizes = [(1440, 1800), (1080, 1350), (750, 938), (640, 800), (1080, 1080), (640, 640), (150, 150)]
    return {
        "pk": pk,
        "id": f"{pk}_99",
        "media_type": 1,
        "taken_at": TAKEN_AT,
        "original_width": 1440,
        "original_height": 1800,
        "image_versions2": {"candidates": [
            {"width": w, "height": h, "url": f"{CDN}/{pk}_{w}x{h}_n.jpg?stp=dst-jpg_e35_p{w}x{h}"}
            for w, h in sizes
        ]},
    }


def video_node(pk="3002", manifest=DASH_WITH_AUDIO):
    return {
        "pk": pk,
        "id": f"{pk}_99",
        "media_type": 2,
        "taken_at": TAKEN_AT,
        "original_width": 1080,
        "original_height": 1920,
        "image_versions2": {"candidates": [{"width": 1080, "height": 1920, "url": f"{CDN}/{pk}_cover.jpg"}]},
        "video_versions": [
            {"type": 101, "width": 720, "height": 1280, "url": f"{CDN}/prog720.mp4?x=1"},
            {"type": 102, "width": 480, "height": 854, "url": f"{CDN}/prog720.mp4?x=2"},  # same file again
            {"type": 103, "width": 480, "height": 854, "url": f"{CDN}/prog480.mp4"},
        ],
        "video_dash_manifest": manifest,
    }


def carousel_media():
    return {
        "pk": "3000",
        "id": "3000_99",
        "code": "Cabc123XYZ_",
        "media_type": 8,
        "product_type": "carousel_container",
        "taken_at": TAKEN_AT,
        "user": {"pk": "99", "username": "someone"},
        "caption": {"text": "Hello world"},
        "carousel_media": [photo_node("3001"), video_node("3002")],
    }


def reel_media():
    media = video_node("4000")
    media.update({"code": "Creel456ABC", "product_type": "clips", "user": {"pk": "99", "username": "someone"},
                  "caption": None})
    return media


def story_reel():
    first = photo_node("5001")
    second = video_node("5002", manifest=None)
    first["taken_at"], second["taken_at"] = TAKEN_AT + 60, TAKEN_AT + 120
    return {"id": "99", "user": {"pk": "99", "username": "someone"}, "items": [first, second]}
