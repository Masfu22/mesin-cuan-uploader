# Mesin Cuan Uploader

Cloud companion for [Masfu22/mesin-cuan](https://github.com/Masfu22/mesin-cuan). It polls the Google Drive queue created by Mesin Cuan, then uploads ready videos to the matching YouTube channel.

## End-to-end flow

```text
GitHub Actions (Masfu22/mesin-cuan)
  -> generate script, narration, footage and MP4
  -> QC and metadata
  -> Google Drive: mesin_cuan/queue/<channel_id>/<job_id>/
       video.mp4
       thumbnail.png       (optional)
       metadata.json
  -> this uploader (Koyeb / Railway / Docker)
  -> matching YouTube channel
  -> Google Drive: mesin_cuan/done/<channel_id>/
```

The queue folder is created by the channel's own Google OAuth identity. The uploader uses that same channel token to read its queue and upload to its YouTube account.

## Deploy

Deploy this repository as a Docker web service on Koyeb or Railway. The root `Dockerfile` listens on port 8080 and runs `python main.py`.

### Required environment variables

Configure these in the uploader hosting provider's environment/secrets page. Use one OAuth token for each channel you want enabled.

| Variable | Value |
|---|---|
| `TOKEN_CH_ID_HORROR` | OAuth authorized-user JSON for channel `ch_id_horror` |
| `TOKEN_CH_ID_PSYCH` | OAuth authorized-user JSON for channel `ch_id_psych` |
| `TOKEN_CH_EN_HORROR` | OAuth authorized-user JSON for channel `ch_en_horror` |
| `TOKEN_CH_EN_PSYCH` | OAuth authorized-user JSON for channel `ch_en_psych` |
| `YOUTUBE_PRIVACY_STATUS` | `private` (default), `unlisted`, or `public` |

A channel whose token variable is empty is skipped. At least one channel token is required for uploads.

### Optional environment variables

| Variable | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram bot token for notifications |
| `TELEGRAM_CHAT_ID` | Telegram destination chat ID |
| `PORT` | Health server port; defaults to `8080` |
| `POLL_INTERVAL_SEC` | Poll interval in seconds; defaults to 300 |

### OAuth token format

Preferred format is the complete OAuth authorized-user JSON produced by Mesin Cuan's `setup_auth.py`. It must contain a refresh token and the Google OAuth fields (`token_uri`, `client_id`, `client_secret`, and scopes).

The uploader also accepts Base64-encoded JSON for compatibility with the Base64 output printed by `setup_auth.py`. It intentionally does **not** accept or unpickle Python pickle files.

Create the tokens from the Mesin Cuan repository, using the matching Google account for each channel:

```bash
python setup_auth.py
```

The OAuth consent must grant:
- YouTube upload: `https://www.googleapis.com/auth/youtube.upload`
- Google Drive file access: `https://www.googleapis.com/auth/drive.file`

Copy each channel's token output into the matching environment variable in the hosting provider. Never put tokens in source code, Git commits, issues, screenshots, or chat messages. If a token is exposed, revoke it in the Google account and generate a new one.

## Queue contract

The uploader expects each queue job folder under:

```text
mesin_cuan/
  queue/
    <channel_id>/
      <job_id>/
        video.mp4
        metadata.json
        thumbnail.png  (optional)
```

The JSON metadata must contain:
- `channel_id`: must exactly match the channel folder being processed
- `status`: must be `ready`
- `title` and `description`
- optional `tags`, `category_id`, `language`, `publish_at`, `made_for_kids`, `contains_ai_content`

The uploader ignores jobs without `metadata.json`, jobs not marked `ready`, and jobs whose `publish_at` is in the future. An invalid non-empty `publish_at` is skipped rather than uploaded early. After a successful upload, the uploader records `status=uploaded` and the YouTube URL in `metadata.json`, then moves the job folder to `mesin_cuan/done/<channel_id>/`. If archiving is interrupted after the marker is saved, the next poll retries the archive step instead of uploading the same video again.

## Privacy and publishing

Uploads default to **private**. This is the safe default and allows a first end-to-end test without accidentally publishing a video.

After reviewing test videos and confirming the channel configuration, set `YOUTUBE_PRIVACY_STATUS=public` if you want the uploader to publish publicly when each job's `publish_at` time arrives. Use `unlisted` if you prefer unlisted videos. The setting applies to every channel handled by this uploader.

## Health check and monitoring

The HTTP health endpoint listens on `PORT` (default 8080) and returns JSON status, last poll time, and daily upload/error counts. Configure the hosting provider's health check to call the service root URL. Run a single service instance/replica so two pollers do not process the same Drive queue simultaneously.

Telegram notifications are optional. The service polls every five minutes by default and waits 60 seconds between uploads.

## Validation

The GitHub Actions validation workflow runs Python syntax compilation and unit tests on pushes and pull requests. A successful CI run checks code-level behavior; it does not prove that Google OAuth, Drive permissions, hosting configuration, or a real YouTube upload works. Those require the user's own credentials and a controlled test video.
