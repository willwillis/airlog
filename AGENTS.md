# Project guidance

## Product scope

- Keep Airlog generic for any direct, FFmpeg-compatible internet audio stream. Do not hardcode or feature a particular station in code, defaults, screenshots, templates, or public documentation.
- `STREAM_NAME` is an optional label used only by the web dashboard and page title. Its default is `Internet Radio`; station-specific names belong in each user's configuration.
- The app records audio, identifies songs, builds play history, and keeps unidentified audio available for review. After the configured grace period, matched audio may be removed automatically; its play history and metadata remain.
- Play history groups adjacent matched chunks for the same track across recorder restarts when their capture times are continuous. Unknown chunks and real time gaps split runs.

## Architecture and configuration

- Keep the three service roles: `recorder`, `worker`, and `web`. They use the same image with different commands and run as separate containers.
- All three containers must use the same host mappings for `/data` and `/audio`. When a shared setting is duplicated in templates, keep its name, default, and behavior consistent across the relevant services.
- Unraid is configured through native XML templates in `templates/`. Do not introduce `.env` files or require Compose for the Unraid workflow. Other Docker users can provide the same settings as environment variables.
- Keep the default matched-audio retention grace period at 1 hour and `AUTO_DELETE_MATCHED` enabled. Unidentified audio is retained for review.
- Capture uses FFmpeg. Song recognition uses ShazamIO as a free, unofficial option; do not add paid APIs or API-key requirements without an explicit product decision.

## Releases and Unraid assets

- A pushed `vMAJOR.MINOR.PATCH` Git tag is the release trigger. The GitHub Actions workflow builds one `linux/amd64` image and publishes both `willisdocker/airlog:<version>` and `willisdocker/airlog:latest` from that build.
- Unraid templates should track `willisdocker/airlog:latest` so Unraid can detect new images. Keep semantic version tags available for users who choose to pin a release.
- Keep Unraid templates under `templates/` and the Docker icon at `icons/airlog-icon.png`. The icon should continue to match the favicon's neon A mark.
- Keep Unraid and preview assets in the repository without copying them into the Docker image unless runtime code needs them.
