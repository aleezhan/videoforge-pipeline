# Publishing

Publishing is a separate command, never a side effect of a pipeline run:

```powershell
uv run videoforge publish <project>/<episode> --to youtube        # dry run: shows what would be sent
uv run videoforge publish <project>/<episode> --to youtube --yes  # actually uploads
```

Rules enforced in code (`videoforge/stages/publish_upload.py`):

- **Nothing is sent by default.** Without `--yes` the command only prints the
  metadata, file and checks.
- **Gates still apply.** An episode waiting for approval, or without a publish
  package (`06_publish/publish.json`), is refused (`--force` overrides the gate).
- **Blocking checks stop the upload.** A failed blocking check in the package
  (for example a platform duration limit) stops the upload unless `--force` is
  given.
- **Idempotent.** The publish state is stored in the episode's `manifest.json`;
  an already uploaded episode is not uploaded twice.

## YouTube (Data API v3)

The YouTube provider (`videoforge/providers/youtube_api.py`) uses a resumable
upload and an OAuth refresh token stored per account in
`.secrets/<account>/youtube_token.json` (git-ignored; values are never printed).

One-time setup per channel, in the Google Cloud console:

1. Create a project and enable **YouTube Data API v3** (and, optionally,
   **YouTube Analytics API** for `account sync`).
2. **Google Auth Platform**: configure the app (audience *External*), add your
   own Google account as a test user.
3. **Data Access**: add the scopes `youtube.upload`, `youtube.force-ssl`,
   `yt-analytics.readonly` and `yt-analytics-monetary.readonly` at once. Google
   does not extend the scopes of an issued token, so adding one later means
   signing in again. `youtube.force-ssl` is the narrowest scope that can change
   an uploaded video (visibility, thumbnail, language).
4. **Clients -> Create client -> Desktop app**, download the JSON.
5. Sign in once; the browser opens and a loopback server on `127.0.0.1`
   receives the token:

   ```powershell
   uv run videoforge account login <account-id> --client-json <downloaded.json>
   ```

Tokens of an app in *Testing* status expire after 7 days; publish the app
(*Audience -> Publish app*) to keep the token until it is revoked.

## TikTok and Instagram

Their upload APIs require an app review, so these platforms are driven through
a browser form with Playwright (`videoforge/providers/browser_publish.py`,
optional extra: `uv sync --extra publish`). `videoforge account login <id>`
opens Chromium with a persistent profile in `accounts/<id>/.profile/`; you sign
in by hand once, and the session is reused for uploads. Form selectors may need
updating when the platforms change their UI.
