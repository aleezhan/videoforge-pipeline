# No dead space — озвучка задаёт длину клипа

> Выдержка из `shared/skills/claymation-ad/prompting/guide.md` репозитория
> [krusemediallc/arcads-claude-code](https://github.com/krusemediallc/arcads-claude-code)
> (MIT, © 2026 Caleb Kruse / Kruse Media LLC). Текст ниже — дословно, кроме этой шапки.
>
> Зачем нам: наша стадия `assemble` замораживает последний кадр, когда озвучка
> длиннее клипа, но не решает обратный случай — клип длиннее озвучки. Здесь
> готовое правило и рецепт: обрезать клип до `озвучка + 0.5s` (0.25s до и после),
> либо удлинить текст; тишину в хвосте не оставлять; озвучку не ускорять atempo.

---

## Narration & dialogue

**Hard rule (confirmed 2026-05-19): Always generate the voiceover externally via ElevenLabs and overlay in post — never use Seedance's in-prompt `Narrator:` line for claymation ads.** The claymation visual is the storytelling vehicle; baking VO into Seedance forces character/lip-sync compromises, produces inconsistent voice quality across beats, and locks pacing to the video model's delivery. ElevenLabs gives one consistent voice across all 7–8 beats, predictable per-line durations, and a clean MP3 to run Whisper against for the caption track.

### Audio pipeline (do this, not in-prompt narrator)

1. **Seedance prompt** — keep ambient SFX language (room tone, distant birdsong, hose hiss, zap clicks) but **omit the `Narrator:` line entirely**. Set `audioEnabled: false` on Seedance (cost is identical, but you avoid generating a stray VO that has to be stripped).
2. **ElevenLabs TTS per beat** — one MP3 per beat using a single consistent `voice_id` across the whole ad.
3. **Trim each clip to match its VO duration — no dead space.** After the TTS returns the MP3, `ffprobe` its duration and trim the matching clip to `lead (0.25s) + vo_dur + tail (0.25s)` before muxing. Don't let the clip ride silent after the VO ends.
4. **Pad each VO mp3** with the 0.25s lead-in and tiny trailing buffer so the audio aligns inside the trimmed clip, then mux (`-c:v copy` on the trimmed video, `-c:a copy` on the padded VO).
5. **Concat the trimmed voiced clips** into the master ad with ffmpeg `-f concat`.
6. **Whisper-transcribe the master VO** and build the captions from word-level timestamps. **Always re-transcribe after trimming** — timestamps shift.

### ⚠️ No dead space — VO drives clip duration

**Hard rule:** the voiceover must fill the full duration of the clip it plays over. Dead space — clip footage continuing after the VO ends, or starting noticeably before the VO begins — kills retention on TikTok/Reels/Shorts. Viewers swipe on the first half-second of silence.

The Seedance default duration (~6–10s) is almost always longer than the TTS line that plays over it. **Measure both, then reconcile.**

```bash
VO_DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 vo/beatN.mp3)
CLIP_DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 clips/beatN.mp4)
TARGET=$(python3 -c "print(min($CLIP_DUR, $VO_DUR + 0.50))")  # lead 0.25 + tail 0.25

# Re-encode-trim the clip (not stream copy — re-encode for a clean cut at the exact target)
ffmpeg -y -i clips/beatN.mp4 -t $TARGET -c:v libx264 -preset slow -crf 18 \
  -pix_fmt yuv420p -c:a copy tight/beatN.mp4
```

**Allowed micro-buffer:** ~0.25s lead-in before VO starts (so the cut doesn't feel jammed) and ~0.25s tail after VO ends (so the cut breath sits before the next beat). Anything beyond that should be filled with more VO content or trimmed out.

**Per-beat: pick one of two options.**

| Option | When | How |
|--------|------|-----|
| **A. Trim the clip to fit the VO** (default) | VO is shorter than clip. Most beats. | Re-encode video to `vo_dur + 0.5s`. Saves runtime, tightens retention. |
| **B. Extend the VO to fill the clip** | Visual has motion that needs the full time to land (camera move, transformation montage, CTA hold). | Add 1–2 more words or a second short line. Re-render the TTS MP3 and re-measure. |

**Never just let the clip ride in silence.** A 6s clip with a 3.5s VO has 2.5s of dead air — pick A or B.

**Hard subrules:**
- If the VO is *longer* than the clip, **never use `atempo` to speed up the VO** — sounds artificial. Instead either split the line across two beats, or re-generate the video clip at a longer duration.
- Caption track must be rebuilt against the *final* trimmed master mp4. Whisper timestamps shift after trimming.
- Re-verify the lower-third caption Y position after trimming — if the trimmed clip ends on a different visual frame than the original, the caption may collide with a foreground element.
