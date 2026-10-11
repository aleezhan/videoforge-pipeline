"""Provider interfaces. Stages depend only on these.

Swapping one backend for another means changing a single file in this package.
Every provider must report the cost of each call (0 for free ones).
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from videoforge.core.storyboard import ChainLink


class ProviderError(RuntimeError):
    pass


class Rejected(ProviderError):
    """The platform ANSWERED with a refusal: the video is known not to be there.

    Used only by publishing, to tell a refusal apart from silence. A dropped
    connection during upload, a timeout or a killed process is not a refusal: the
    file may have been accepted and the response lost, and a retry would put a
    duplicate on the channel, which no local fix can undo. So the outcome is
    UNKNOWN by default, and `Rejected` is raised only by code holding the
    platform's actual answer: an HTTP error code, a form rejection, a failed
    pre-upload validation.

    Hence the adapter must wrap its own transport errors (`httpx.HTTPError`,
    Playwright timeouts): the stage knows nothing about transport and must not
    judge by exception type."""


@dataclass
class GenResult:
    path: Path
    cost_usd: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Reference:
    """A reference image labelled with what it shows: the model must know who or
    what each image refers to, or several references in one request get mixed up.

    `role` is the image's purpose: `cast` is a portfolio of a character, prop or
    location (appearance must match); `composition` is a "should look like this"
    image for a scene (`Scene.reference_assets`), supplying layout and perspective
    while objects and style come from the prompt. A LoRA-based image model does
    not take cast portfolios (the weights hold the face) but does accept
    composition references."""

    label: str  # in English: "character 'timur'", "prop 'compass'", "location 'forest'"
    path: Path
    role: str = "cast"  # cast | composition


@dataclass(frozen=True)
class MotionGuide:
    """A scene's motion reference (`Scene.motion_reference`), already resolved on
    disk: the provider gets an absolute path rather than a storyboard string. The
    stage decides where to find the file; the provider only guides the clip by it.

    `signal` is what to extract from the reference (`storyboard.MotionSignal`:
    pose | canny | depth | raw); `strength` is the guidance adapter strength 0..1.
    The file may be any length: the provider trims or extends it to the scene's
    8n+1 frames, because frame count is the provider's rule, not the stage's."""

    path: Path
    signal: str
    strength: float = 1.0


@dataclass
class CheckResult:
    passed: bool
    issues: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    raw: str = ""
    # The checker REFUSED to look: no check took place, which is neither a frame
    # failure nor a pass. Safety filters may block a request probabilistically (an
    # empty answer with block_reason and no HTTP error). Counting that as a failure
    # burns regenerations on a fine frame; counting it as a pass claims "checked"
    # for something nobody looked at. Non-empty means the provider's reason
    # ("block_reason=SAFETY"); the stage marks the asset `skipped` with this note.
    refused: str = ""


@dataclass
class LlmResult:
    text: str
    cost_usd: float = 0.0


@dataclass(frozen=True)
class FrameToReview:
    """A frame to review AGAINST ITS DESCRIPTION: does it show what the scene asked for.
    Unlike `check_images` (one frame vs cast references and a checklist), this
    reviews dozens of frames in one call and returns verdicts in words."""

    id: str  # scene id
    path: Path
    description: str  # the scene's visual_prompt
    narration: str = ""
    # per-frame channel rules (project `video.frame_rules`): a violation is a defect,
    # just like an off-topic frame
    rules: tuple[str, ...] = ()


@dataclass
class FrameVerdict:
    """What the model said about one frame of a batch.

    `severity`: `ok` - the frame matches; `note` - minor mismatch, flagged for the
    human at the gate; `block` - wrong object, composition contradicts the
    description, extra people or objects change the meaning, garbled pseudo-text,
    etc.; the frame is redone. `fix` is a one-sentence repair hint appended to the
    description on retry. `search` is a query for a reference image about the
    COMPOSITION (layout, perspective), not the scene's subject."""

    id: str
    match: bool
    severity: str = "ok"  # ok | note | block
    see: str = ""
    issue: str = ""
    fix: str = ""
    search: str = ""


@dataclass
class ReviewResult:
    verdicts: list[FrameVerdict]
    cost_usd: float = 0.0
    raw: str = ""


@dataclass(frozen=True)
class ImageCandidate:
    """An image found in an open source: file URL, source page and license, so the
    reference's provenance is recorded next to it."""

    url: str
    title: str = ""
    page: str = ""
    license: str = ""
    width: int = 0
    height: int = 0


class ImageSearchProvider(ABC):
    """Reference-image search by query (`features/scene_reference_image`).

    Used by the images stage when a frame does not match its description: instead
    of blindly re-rolling the seed, the retry uses an image that already has the
    desired composition. A model picks from a contact sheet of candidates; the
    provider only searches and downloads."""

    name: str = "?"

    @abstractmethod
    def search(self, query: str, *, limit: int = 8) -> list[ImageCandidate]: ...

    @abstractmethod
    def fetch(self, candidate: ImageCandidate, out_path: Path) -> Path:
        """Download the candidate to `out_path` (extension follows the source file)."""


@dataclass
class TranscriptResult:
    words_srt: Path  # per-word timings, same format the TTS stage writes (edge-tts)
    language: str = ""
    cost_usd: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)


class TranscriptProvider(ABC):
    """Speech to timed words. Word timings are the provider's job: some services
    return them with auto-captions, others (e.g. Whisper) compute them. Stages always
    get the same per-word .srt, wherever it came from."""

    name: str = "?"

    @abstractmethod
    def transcribe(
        self,
        *,
        url: str,
        video: Path | None,
        language: str,
        duration_sec: float,
        out_path: Path,
    ) -> TranscriptResult:
        """`url` is the video link (for services with ready captions), `video` a local
        file (for speech recognition); the provider uses whichever it supports."""
        ...


@dataclass
class LyricsResult:
    """Song lyrics as plain TEXT, with no timing claims.

    Timings are deliberately dropped even when the service provides them: they come
    from the studio release, while we cut live performances where the same line
    lands at a different second. Words come from here, seconds from our own audio."""

    text: str  # line by line, as sung
    source: str = ""  # where it came from: service and what exactly was found
    cost_usd: float = 0.0


class LyricsProvider(ABC):
    """Song title to lyrics, so the user need not supply them: speech recognition
    garbles sung words and there is nothing else to check them against."""

    name: str = "?"

    @abstractmethod
    def fetch(self, *, artist: str, track: str, duration_sec: float = 0.0) -> LyricsResult | None:
        """None means not found. That is a normal answer, not an error (rare track,
        live version, custom mix). Words then rely on speech recognition, and that
        must be reported explicitly."""
        ...


class ImageProvider(ABC):
    name: str = "?"
    price_usd: float = 0.0  # per image

    def weights(self) -> str | None:
        """Configuration beyond the prompt and provider name that directly affects
        the frame: weights not visible in the prompt. For self-hosted ComfyUI this
        is the character LoRA and its strength as actually DEPLOYED on the pod (not
        as recorded on the character), since that is what renders. The string is
        part of the frame fingerprint (`ImagesStage._inputs`): changed weights make
        the frame stale, like a provider change. Empty means no such weights.
        `None` means it cannot be determined now (GPU not running): the stage then
        skips the input comparison and keeps existing frames."""
        return ""

    @abstractmethod
    def generate(
        self, prompt: str, *, references: list[Reference], aspect_ratio: str, out_path: Path
    ) -> GenResult: ...


class VideoProvider(ABC):
    name: str = "?"
    supported_durations: tuple[int, ...] = (6,)
    # The provider moves the camera itself (programmatic zoom over a still, e.g.
    # remotion) and can start a continuation clip where the previous link's move
    # ended (`chain` in `animate`). Generative (i2v) providers cannot: the model
    # drives motion, each generation starts from the frame as-is, and assembly
    # stitches continuations with a crop (`video.zoom_per_sec`). The video stage
    # uses this flag to decide who receives the chain position and whose input
    # fingerprint depends on it; for generative clips the chain is not an input,
    # otherwise editing a neighbour's length would force a paid regeneration.
    chains_camera: bool = False
    # The provider can guide a clip by a motion reference (`MotionGuide` in
    # `animate`), e.g. a ComfyUI control adapter. The video stage uses this flag to
    # refuse BEFORE the first paid call when a scene has a reference the provider
    # cannot read. Such providers raise ProviderError in `animate` anyway (there is
    # no "prompt-only" fallback; it would be a different video), but learning that
    # after paying for neighbouring scenes is too late.
    follows_reference: bool = False

    def not_ready(self) -> str:
        """Human-readable reason the provider cannot render any clip right now;
        empty if ready or not checkable. Not network or key problems, but missing
        local setup (for remotion: Node.js and `npm install`).

        The video stage checks this before the first paid call: still scenes
        (`Scene.render: still`) go to a second provider interleaved with clips, and
        discovering a missing Node.js at scene forty, after paid clips, is too late.
        `doctor` reports the same."""
        return ""

    def price_for(self, duration: int, *, guided: bool = False) -> float:
        """Clip price for the budget guard. `guided` means a motion-referenced clip,
        which may cost more (annotator plus control tokens); providers without
        reference support return the same price."""
        return 0.0

    def pick_duration(self, wanted: float) -> int:
        """Nearest supported duration that is not shorter than the wanted one."""
        for d in sorted(self.supported_durations):
            if d >= wanted - 1e-6:
                return d
        return max(self.supported_durations)

    @abstractmethod
    def animate(
        self,
        image: Path,
        prompt: str,
        *,
        duration_sec: int,
        aspect_ratio: str,
        out_path: Path,
        chain: ChainLink | None = None,
        motion_reference: MotionGuide | None = None,
    ) -> GenResult:
        """Frame plus motion description to a clip of `duration_sec` seconds.

        `chain` is the scene's position in a chain of continuations of one shot;
        passed only to providers with `chains_camera`.

        `motion_reference` is a skeleton or video to guide motion; passed only to
        providers with `follows_reference`. A provider that cannot use it must raise
        ProviderError rather than silently render from the prompt: the user set the
        reference for a specific action, and a clip without it is a defect that frame
        checks do not catch."""


class TtsProvider(ABC):
    name: str = "?"
    extension: str = ".mp3"
    price_per_char: float = 0.0
    takes_context = False  # uses neighbouring text (`context`) to continue its intonation

    @abstractmethod
    def synthesize(
        self,
        text: str,
        *,
        voice: str,
        rate: str,
        pitch: str,
        out_path: Path,
        volume: str = "+0%",
        context: tuple[str, str] | None = None,
    ) -> GenResult:
        """Synthesize `text`. `rate`, `pitch`, `volume` use edge-tts format
        ("+10%", "-4Hz"): defaults come from the voice resource, overrides from the
        scene (`Scene.voice`). Providers without volume control ignore it: it is a
        nuance of delivery, not meaning.

        `context` is (text before, text after): not spoken, but the phrase's
        intonation continues it. Ignored when `takes_context` is False."""


class LlmProvider(ABC):
    name: str = "?"
    # UPPER-BOUND estimate of one frame check, in USD. The real price arrives in the
    # response (computed from usage), but the budget guard needs a number BEFORE the
    # call: zero would mean "free" and let the call through an exhausted budget. Like
    # other prices, it is a reference value reconciled with the price list by hand.
    check_price_usd: float = 0.0

    @abstractmethod
    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_mode: bool = False,
        images: list[Path] | None = None,
    ) -> LlmResult:
        """Text completion, optionally with input images.

        `images` is for free-form "look and describe" analysis; for production
        pass/fail checks use check_images."""
        ...

    @abstractmethod
    def check_images(
        self, images: list[Path], *, references: list[Reference], checklist: list[str], context: str
    ) -> CheckResult:
        """Compare frames with references and a checklist. Videos pass several frames."""
        ...

    # Whether the provider can review frames IN BATCH against descriptions
    # (`review_frames`). The images stage checks this flag instead of catching an
    # error: without it the description review is skipped, visibly in the message.
    reviews_frames: bool = False

    def review_frames(self, frames: list[FrameToReview]) -> ReviewResult:
        """Dozens of frames in one call: does each show what the scene asked for,
        what is wrong, how to fix it and what reference-search query to use.

        Batched rather than one by one because per-call process startup is paid only
        once, which more than halves total review time. Providers that cannot do
        this leave `reviews_frames = False`."""
        raise ProviderError(f"{self.name} не проверяет кадры пачкой по описанию")


@dataclass
class PublishMeta:
    """Metadata sent to the platform with the file, built from `06_publish/publish.json`.

    `synthetic` is the "AI-generated" label: platforms require it for generated
    voice and imagery, and it must be set at upload time, not remembered later. A
    provider that cannot set such a field must emit a warning rather than stay
    silent."""

    platform: str  # youtube | tiktok | instagram
    title: str
    description: str = ""
    hashtags: list[str] = field(default_factory=list)
    privacy: str = "private"  # private | unlisted | public; not public by default
    made_for_kids: bool = False
    synthetic: bool = True
    account_id: str = ""  # account from accounts/ whose session uploads the video
    language: str = "ru"  # language of the SPEECH in the video
    # Language of title, description and tags; separate from speech because a clips
    # channel may have speech in one language and text in another, and the platform
    # uses this field to choose the audience. Empty means same as the speech.
    text_language: str = ""
    # custom thumbnail image path. Empty lets the platform pick a frame itself, which
    # is often an unflattering mid-word frame
    thumbnail: str = ""
    # topic category AS A PLATFORM ID ("17" on YouTube): our own name
    # (`core.project.CATEGORIES`) is mapped to it before the provider, since each
    # platform has its own catalogue. Empty means the project declared none.
    category: str = ""

    def tagged_description(self) -> str:
        """Description with hashtags appended at the end, as all three platforms accept.

        A hashtag already present in the description text or title is not appended
        again: the model tends to inline hashtags despite being asked to list them
        separately, which would otherwise duplicate them. A title made only of
        hashtags is also taken into account."""
        present = {m.lower() for m in re.findall(r"#(\w+)", f"{self.title} {self.description}")}
        tags = " ".join(
            f"#{t.lstrip('#')}" for t in self.hashtags if t.lstrip("#").lower() not in present
        )
        return f"{self.description}\n\n{tags}".strip() if tags else self.description.strip()


@dataclass
class PublishResult:
    """Upload outcome. Published means `state == "done"`, not "has an id": some
    upload forms never return a video id, and relying on it would mean uploading
    the short a second time."""

    platform: str
    state: str = "done"  # done | pending | failed | dry_run
    video_id: str = ""
    url: str = ""
    cost_usd: float = 0.0
    request_id: str = ""  # for platforms with asynchronous uploads (check_status)
    warnings: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


class PublishProvider(ABC):
    """File plus packaging to a video on a platform.

    Like other providers it reports call cost (zero for official APIs and browser
    sessions, which cost quota and time rather than money). No provider uploads
    anything on its own: a human decides via `videoforge publish ... --yes`."""

    name: str = "?"
    platforms: tuple[str, ...] = ()  # supported networks: youtube | tiktok | instagram

    @abstractmethod
    def upload(self, video_path: Path, meta: PublishMeta) -> PublishResult: ...

    def check_status(self, request_id: str) -> PublishResult:
        """What the platform did with an uploaded video. `request_id` is the video id
        (or the request id for platforms with asynchronous uploads). Read-only.

        `state`: done - processed and live; pending - accepted, still processing;
        failed - rejected, broken or no video with that id. Details go in `meta` in
        the platform's own words, for humans rather than code: `found` (whether the
        video exists), `upload_status`, `processing`, `reason` (the platform's stated
        reason), `privacy`, `notes` (what the API does not expose). The state
        vocabulary is YouTube's (`landed_state`): other platforms do not expose one
        via API, and the stub speaks the same vocabulary."""
        raise ProviderError(f"{self.name}: проверка состояния не поддерживается")

    def find_video(self, title: str, *, since: datetime) -> list[PublishResult]:
        """Own videos with this title uploaded after `since`.

        Needed for exactly one case: the file was sent but no response came. The
        video id arrives in that very response, so only the title and start time
        are known. An empty list means the platform never received it; several
        results mean it was received more than once and a human decides which."""
        raise ProviderError(f"{self.name}: искать свои ролики по названию не умеет")

    def describe(self, video_id: str) -> dict[str, str]:
        """Current state of the video on the platform, as flat strings.

        Lets an update show "before -> after" BEFORE changing anything. A platform
        that cannot read uploaded videos must say so (raise): an empty dict would
        read as "everything matches"."""
        raise ProviderError(f"{self.name}: прочитать загруженный ролик не умеет")

    def update(self, video_id: str, meta: PublishMeta) -> PublishResult:
        """Bring an already published video in line with its packaging: visibility,
        text, languages, thumbnail. The file is not re-uploaded, so the channel keeps
        the same video with the same link and views."""
        raise ProviderError(f"{self.name}: правку загруженного ролика не умеет")


def landed_state(upload_status: str, processing: str) -> str:
    """Map platform upload/processing statuses to done | pending | failed.

    Vocabulary is YouTube's (`uploadStatus`, `processingStatus`): uploaded ->
    processed | failed | rejected | deleted, and processing -> succeeded | failed |
    terminated. Failure is checked first: "processed" can coexist with
    "terminated", and that is not live."""
    if upload_status in ("failed", "rejected", "deleted") or processing in ("failed", "terminated"):
        return "failed"
    if upload_status == "processed" or processing == "succeeded":
        return "done"
    return "pending"


@dataclass
class ProviderSet:
    image: ImageProvider
    video: VideoProvider
    tts: TtsProvider
    llm: LlmProvider
    # The checker reviews frames and clips (`check_images`), separately from the text
    # writer. A single shared slot made frame checks impossible when the text LLM
    # (e.g. a subscription CLI) cannot review images in production, leaving no one
    # checking frames. None means "same as `llm`": a configuration default, not an
    # error fallback (a crashing checker fails the stage like any provider). Stored
    # separately from the `check` property because the set is patched piecemeal
    # after construction (`providers.llm = Picky()` in tests), and checks must go
    # to whoever occupies the slot NOW.
    checker: LlmProvider | None = None
    # reference-image search for frames that do not match their description
    # (`VF_SEARCH_PROVIDER`, default Wikimedia Commons, no key). None means no
    # search: the retry runs without a reference and the stage says so.
    search: ImageSearchProvider | None = None
    # Renders still scenes (`Scene.render: still`): a finished frame with a
    # programmatic zoom or pan instead of a generated clip, used when the project's
    # clip share is below 100% (`video.clip_share`). `VF_STILL_PROVIDER`, default
    # remotion ($0). Not a fallback: scenes are assigned up front and a crashing
    # provider fails the stage. None means not configured (hand-built set): the
    # stage refuses at the first still scene before spending, rather than hand it
    # to `video`.
    still: VideoProvider | None = None

    @property
    def check(self) -> LlmProvider:
        """Who checks frames: the dedicated checker, or `llm` if none is set."""
        return self.checker if self.checker is not None else self.llm

    def names(self) -> dict[str, str]:
        names = {
            "image": self.image.name,
            "video": self.video.name,
            "tts": self.tts.name,
            "llm": self.llm.name,
            "check": self.check.name,
        }
        if self.search is not None:
            names["search"] = self.search.name
        if self.still is not None:
            names["still"] = self.still.name
        return names
