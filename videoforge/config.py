"""Environment and provider selection. The only place that reads `.env`.

Keys are never printed: only their presence is checked (`has_key`).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENV_FILE = ".env"


def load_env(root: Path) -> None:
    """Load KEY=value pairs from `<root>/.env` into the environment without
    overriding variables that are already set."""
    path = root / ENV_FILE
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def has_key(name: str) -> bool:
    return bool(os.environ.get(name))


def intel_llm_provider() -> str:
    """LLM for analysis stages. Defaults to claude_cli, which runs on a subscription."""
    return os.environ.get("VF_INTEL_LLM_PROVIDER", "claude_cli")


def vision_check_enabled() -> bool:
    """Whether frames and clips are checked by a vision model (images/video stages).

    `VF_VISION_CHECK=off` disables it: scenes get `checks: skipped` with a note and
    a human decides at the gate. Useful when the LLM provider cannot check frames
    or the checker is unavailable (quota exhausted); otherwise the paid stage would
    fail on the very first scene."""
    return os.environ.get("VF_VISION_CHECK", "on").strip().lower() not in (
        "off",
        "0",
        "false",
        "no",
    )


def frame_review_enabled() -> bool:
    """Whether frames are batch-reviewed AGAINST DESCRIPTIONS (images stage,
    `LlmProvider.review_frames`).

    `VF_FRAME_REVIEW=off` disables it. Separate from `VF_VISION_CHECK`, which is the
    checker comparing frames with cast references; this asks the script-writing LLM
    "does the frame show what the scene asked for". An off-topic frame is retried
    with a found reference image (`features/scene_reference_image`)."""
    return os.environ.get("VF_FRAME_REVIEW", "on").strip().lower() not in (
        "off",
        "0",
        "false",
        "no",
    )


def runpod_key_var() -> str:
    """Name of the env var holding the RunPod key, allowing a second account (e.g.
    `RUNPOD_API_KEY_BENCH`). The key itself is read only in providers/runpod_pod.py."""
    return os.environ.get("VF_RUNPOD_KEY_VAR", "RUNPOD_API_KEY")


@dataclass(frozen=True)
class ProviderNames:
    image: str
    video: str
    tts: str
    llm: str
    # frame/clip checker (`VF_CHECK_PROVIDER`). Empty means the same as llm: a
    # configuration default, not a fallback. Needed when text goes through a
    # provider that does not review images in production (e.g. claude_cli on a
    # subscription), so checks are delegated to a cheap vision model instead.
    check: str = ""
    # reference-image search for frames that do not match their description
    # (`VF_SEARCH_PROVIDER`): commons - Wikimedia Commons, no key; mock - stub;
    # none - no search (retry runs without a reference)
    search: str = "commons"
    # still scenes (`VF_STILL_PROVIDER`): a frame with a programmatic zoom instead of
    # a generated clip for `render: still` scenes, when the project clip share is
    # below 100%. remotion - $0, any length; mock - stub
    still: str = "remotion"

    @classmethod
    def mock(cls) -> ProviderNames:
        return cls(image="mock", video="mock", tts="mock", llm="mock", search="mock", still="mock")

    @classmethod
    def from_env(cls) -> ProviderNames:
        return cls(
            image=os.environ.get("VF_IMAGE_PROVIDER", "gemini"),
            video=os.environ.get("VF_VIDEO_PROVIDER", "fal"),
            tts=os.environ.get("VF_TTS_PROVIDER", "edge"),
            llm=os.environ.get("VF_LLM_PROVIDER", "gemini"),
            check=os.environ.get("VF_CHECK_PROVIDER", "").strip(),
            search=os.environ.get("VF_SEARCH_PROVIDER", "commons").strip() or "none",
            still=os.environ.get("VF_STILL_PROVIDER", "remotion").strip() or "remotion",
        )

    def as_dict(self) -> dict[str, str]:
        """Provider names per slot, with the checker resolved (empty -> llm name).
        The CLI uses this to ask for keys of providers the stages will call."""
        return {
            "image": self.image,
            "video": self.video,
            "tts": self.tts,
            "llm": self.llm,
            "check": self.check or self.llm,
            "still": self.still,
        }
