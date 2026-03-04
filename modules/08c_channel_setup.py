"""
VideoForge — Module 08c: YouTube Channel Branding Setup.

Sets channel branding via YouTube Data API v3 (everything that's possible):
  ✅  Description       — channels.update (brandingSettings.channel.description)
  ✅  Keywords / tags   — channels.update (brandingSettings.channel.keywords)
  ✅  Country           — channels.update (brandingSettings.channel.country)
  ✅  Banner image      — generate via WaveSpeed → channelBanners.insert → channels.update
  ✅  Channel trailer   — channels.update (brandingSettings.channel.unsubscribedTrailer)
  ⚠️  Avatar/logo       — generated via WaveSpeed → saved locally → manual upload in Studio
  ❌  Channel name      — YouTube Studio only (API returns 403)

Branding config (in channel_config JSON):

    "branding": {
        "description":    "Full channel description text (max 1000 chars)",
        "keywords":       ["history", "world history", "historical events"],
        "country":        "UA",
        "banner_prompt":  "Epic historical collage, ancient Rome, Egypt, renaissance, cinematic ...",
        "avatar_prompt":  "Bold letter H on dark background, gold metallic, minimalist logo ...",
        "banner_path":    "",   ← pre-made banner (skips generation if set)
        "avatar_path":    "",   ← pre-made avatar (skips generation if set)
        "trailer_video_id": ""
    }

Image specs:
    Banner: 2560×1440 px recommended (YouTube safe area: 1546×423 px center)
    Avatar: 800×800 px (displayed as circle, keep subject centered)

CLI:
    # Generate banner + avatar from prompts, then apply everything
    python modules/08c_channel_setup.py \\
        --channel config/channels/history.json \\
        --channel-name main \\
        --generate

    # Use pre-made banner file (no generation)
    python modules/08c_channel_setup.py \\
        --channel config/channels/history.json \\
        --channel-name main \\
        --banner assets/branding/banner.png

    # Only generate images, don't upload to YouTube yet
    python modules/08c_channel_setup.py \\
        --channel config/channels/history.json \\
        --channel-name main \\
        --generate --no-upload

    # Dry run — show what would be done
    python modules/08c_channel_setup.py \\
        --channel config/channels/history.json \\
        --channel-name main \\
        --generate --dry-run

Notes:
    - Banner generation: WaveSpeed 2560×1440 → auto-uploaded via YouTube API
    - Avatar generation: WaveSpeed 800×800 → saved to assets/branding/ → upload manually in Studio
    - Banner max file size: 6 MB (WaveSpeed PNG is usually 2-4 MB at this resolution)
    - Keywords: comma-separated in CLI; multi-word tags auto-quoted in API format
    - Country: ISO 3166-1 alpha-2 (UA, US, GB, DE ...)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from clients.youtube_auth import get_youtube_service_from_config
from clients.wavespeed_client import WaveSpeedClient
from modules.common import load_channel_config, load_env, setup_logging
from googleapiclient.http import MediaFileUpload

log = setup_logging("channel_setup")

# ─── Constants ────────────────────────────────────────────────────────────────

MAX_DESCRIPTION  = 1000   # YouTube limit
MAX_KEYWORDS     = 500    # YouTube limit (total chars)
BANNER_SIZE      = "2560*1440"   # YouTube recommended channel art size
AVATAR_SIZE      = "800*800"     # Square, displayed as circle
MIN_FILE_BYTES   = 10_000
BRANDING_DIR     = ROOT / "assets" / "branding"


# ─── Image generation ─────────────────────────────────────────────────────────

async def _generate_image(
    prompt:   str,
    out_path: Path,
    size:     str,
    label:    str,
) -> bool:
    """Generate a single image via WaveSpeed. Returns True on success."""
    log.info("Generating %s (%s)...", label, size)
    try:
        async with WaveSpeedClient() as wave:
            await wave.generate_text2img(
                prompt,
                size=size,
                seed=42,
                num_inference_steps=4,
                output_path=out_path,
            )
        if out_path.exists() and out_path.stat().st_size >= MIN_FILE_BYTES:
            log.info("%s generated: %s (%.1f MB)", label, out_path.name,
                     out_path.stat().st_size / 1_048_576)
            return True
        log.warning("%s: file too small or missing after generation", label)
        return False
    except Exception as exc:
        log.error("%s generation failed: %s", label, exc)
        return False


async def generate_assets(
    branding_cfg: dict,
    out_dir:      Path,
    dry_run:      bool = False,
) -> dict[str, Path | None]:
    """
    Generate banner and avatar images using WaveSpeed.

    Returns:
        {"banner": Path | None, "avatar": Path | None}
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Path | None] = {"banner": None, "avatar": None}

    banner_prompt = branding_cfg.get("banner_prompt", "").strip()
    avatar_prompt = branding_cfg.get("avatar_prompt", "").strip()

    if not banner_prompt and not avatar_prompt:
        log.warning("No banner_prompt or avatar_prompt in branding config — skipping generation")
        return result

    if dry_run:
        if banner_prompt:
            log.info("[DRY RUN] Would generate banner (%s): %s...", BANNER_SIZE, banner_prompt[:60])
        if avatar_prompt:
            log.info("[DRY RUN] Would generate avatar (%s): %s...", AVATAR_SIZE, avatar_prompt[:60])
        return result

    tasks = []
    if banner_prompt:
        banner_path = out_dir / "banner.png"
        tasks.append(("banner", banner_prompt, banner_path, BANNER_SIZE))
    if avatar_prompt:
        avatar_path = out_dir / "avatar.png"
        tasks.append(("avatar", avatar_prompt, avatar_path, AVATAR_SIZE))

    # Generate concurrently
    coros = [
        _generate_image(prompt, path, size, label)
        for label, prompt, path, size in tasks
    ]
    done = await asyncio.gather(*coros, return_exceptions=True)

    for (label, prompt, path, size), success in zip(tasks, done):
        if success is True and path.exists():
            result[label] = path
            log.info("%s saved: %s", label.capitalize(), path)
        else:
            log.warning("%s generation failed or returned exception: %s", label, success)

    return result


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _format_keywords(keywords: list[str]) -> str:
    """
    Format list of keywords into YouTube's space-separated string.
    Multi-word keywords are double-quoted.

    Example:
        ["history", "world history", "ancient rome"]
        → 'history "world history" "ancient rome"'
    """
    parts: list[str] = []
    for kw in keywords:
        kw = kw.strip()
        if not kw:
            continue
        if " " in kw:
            parts.append(f'"{kw}"')
        else:
            parts.append(kw)
    result = " ".join(parts)
    if len(result) > MAX_KEYWORDS:
        log.warning("Keywords string too long (%d chars), truncating to %d", len(result), MAX_KEYWORDS)
        result = result[:MAX_KEYWORDS]
    return result


def _get_channel_id(service) -> str | None:
    """Fetch the authenticated channel's ID."""
    resp = service.channels().list(part="id", mine=True).execute()
    items = resp.get("items", [])
    if not items:
        return None
    return items[0]["id"]


# ─── Banner upload ─────────────────────────────────────────────────────────────

def _upload_banner(service, banner_path: Path) -> str | None:
    """
    Upload banner image and return the bannerExternalUrl.

    Minimum size: 2048×1152 px | Max: 6 MB | Formats: PNG, JPG
    Returns the URL string on success, None on failure.
    """
    if not banner_path.exists():
        log.error("Banner file not found: %s", banner_path)
        return None

    size_mb = banner_path.stat().st_size / 1_048_576
    if size_mb > 6:
        log.error("Banner too large: %.1f MB (max 6 MB)", size_mb)
        return None

    mime = "image/png" if banner_path.suffix.lower() == ".png" else "image/jpeg"
    log.info("Uploading banner to YouTube: %s (%.1f MB)...", banner_path.name, size_mb)

    try:
        media = MediaFileUpload(str(banner_path), mimetype=mime, resumable=False)
        resp = service.channelBanners().insert(media_body=media).execute()
        url = resp.get("url")
        if url:
            log.info("Banner uploaded OK")
            return url
        log.error("Banner upload returned no URL: %s", resp)
        return None
    except Exception as exc:
        log.error("Banner upload failed: %s", exc)
        return None


# ─── Main branding update ──────────────────────────────────────────────────────

def apply_branding(
    service,
    channel_id:  str,
    description: str | None       = None,
    keywords:    list[str] | None = None,
    country:     str | None       = None,
    banner_url:  str | None       = None,
    trailer_id:  str | None       = None,
    dry_run:     bool             = False,
) -> dict[str, Any]:
    """
    Apply branding settings to the channel via channels.update.

    Returns a dict of what was (or would be) changed.
    """
    channel_branding: dict[str, Any] = {}

    if description is not None:
        safe_desc = description[:MAX_DESCRIPTION]
        if len(description) > MAX_DESCRIPTION:
            log.warning("Description truncated to %d chars", MAX_DESCRIPTION)
        channel_branding["description"] = safe_desc
        log.info("  description: %s...", safe_desc[:60])

    if keywords is not None:
        kw_str = _format_keywords(keywords)
        channel_branding["keywords"] = kw_str
        log.info("  keywords: %s", kw_str[:80])

    if country is not None:
        channel_branding["country"] = country.upper()
        log.info("  country: %s", country.upper())

    if trailer_id:  # skip empty string
        channel_branding["unsubscribedTrailer"] = trailer_id
        log.info("  trailer video ID: %s", trailer_id)

    body: dict[str, Any] = {
        "id": channel_id,
        "brandingSettings": {
            "channel": channel_branding,
        },
    }

    # Banner lives in brandingSettings.image, not .channel
    if banner_url is not None:
        body["brandingSettings"]["image"] = {"bannerExternalUrl": banner_url}
        log.info("  banner URL: set")

    if not channel_branding and banner_url is None:
        log.warning("Nothing to update — no branding fields provided")
        return {}

    if dry_run:
        log.info("[DRY RUN] Would call channels.update with: %s",
                 json.dumps(body, ensure_ascii=False, indent=2))
        return body

    try:
        resp = service.channels().update(
            part="brandingSettings",
            body=body,
        ).execute()
        log.info("Channel branding updated (channel_id=%s)", channel_id)
        return resp
    except Exception as exc:
        log.error("channels.update failed: %s", exc)
        raise


# ─── Orchestrator ─────────────────────────────────────────────────────────────

async def setup_channel(
    channel_config_path: str | Path,
    channel_name:        str | None = None,
    description:         str | None = None,
    keywords:            list[str] | None = None,
    country:             str | None = None,
    banner_path:         str | Path | None = None,
    trailer_video_id:    str | None = None,
    generate:            bool = False,
    no_upload:           bool = False,
    dry_run:             bool = False,
) -> dict[str, Any]:
    """
    Main entry point:
      1. Optionally generate banner + avatar via WaveSpeed
      2. Authenticate YouTube
      3. Upload banner to YouTube (channelBanners.insert)
      4. Apply all branding settings (channels.update)
      5. Print summary + avatar upload instructions

    CLI overrides always take precedence over channel_config["branding"].
    """
    load_env()
    channel_config = load_channel_config(channel_config_path)

    ch_name = (
        channel_name
        or channel_config.get("channel_name", "")
        or "channel"
    ).replace(" ", "_").lower()

    # Merge config + CLI (CLI wins)
    branding_cfg: dict = channel_config.get("branding", {})

    final_description   = description    or branding_cfg.get("description")
    final_keywords      = keywords       or branding_cfg.get("keywords")
    final_country       = country        or branding_cfg.get("country")
    final_trailer_id    = trailer_video_id or branding_cfg.get("trailer_video_id") or None

    # Banner path: CLI > config banner_path > generated
    banner_override   = Path(banner_path) if banner_path else None
    banner_from_cfg   = branding_cfg.get("banner_path", "").strip()
    final_banner_path = banner_override or (Path(banner_from_cfg) if banner_from_cfg else None)

    # Avatar path: config avatar_path > generated
    avatar_from_cfg   = branding_cfg.get("avatar_path", "").strip()
    final_avatar_path: Path | None = Path(avatar_from_cfg) if avatar_from_cfg else None

    # ── Step 1: Generate banner + avatar via WaveSpeed ────────────────────────
    if generate:
        log.info("Generating branding assets via WaveSpeed...")
        generated = await generate_assets(branding_cfg, BRANDING_DIR, dry_run=dry_run)
        if generated.get("banner") and not final_banner_path:
            final_banner_path = generated["banner"]
        if generated.get("avatar") and not final_avatar_path:
            final_avatar_path = generated["avatar"]

    # ── Step 2: Authenticate YouTube ──────────────────────────────────────────
    log.info("Setting up channel branding for '%s'...", ch_name)
    service = get_youtube_service_from_config(ch_name, channel_config)

    channel_id = _get_channel_id(service)
    if not channel_id:
        raise RuntimeError("Could not determine channel ID")
    log.info("Channel ID: %s", channel_id)

    # ── Step 3: Upload banner → get URL ───────────────────────────────────────
    banner_url: str | None = None
    if not no_upload and final_banner_path:
        if dry_run:
            log.info("[DRY RUN] Would upload banner: %s", final_banner_path)
            banner_url = "https://yt3.ggpht.com/placeholder_banner_url"
        else:
            banner_url = _upload_banner(service, final_banner_path)

    # ── Step 4: channels.update ───────────────────────────────────────────────
    kw_list: list[str] | None = None
    if final_keywords is not None:
        if isinstance(final_keywords, list):
            kw_list = final_keywords
        else:
            kw_list = [kw.strip() for kw in str(final_keywords).split(",") if kw.strip()]

    result = apply_branding(
        service,
        channel_id=channel_id,
        description=final_description,
        keywords=kw_list,
        country=final_country,
        banner_url=banner_url,
        trailer_id=final_trailer_id,
        dry_run=dry_run,
    )

    # ── Step 5: Summary ───────────────────────────────────────────────────────
    _print_summary(ch_name, channel_id, result, final_avatar_path, dry_run)
    return result


# ─── Print summary ────────────────────────────────────────────────────────────

def _print_summary(
    ch_name:      str,
    channel_id:   str,
    result:       dict,
    avatar_path:  Path | None,
    dry_run:      bool,
) -> None:
    import io
    out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    prefix = "[DRY RUN] " if dry_run else ""
    out.write("\n" + "=" * 65 + "\n")
    out.write(f"  {prefix}Channel Branding — {'preview' if dry_run else 'done'}\n")
    out.write("=" * 65 + "\n")
    out.write(f"  Channel: {ch_name} (id={channel_id})\n")

    bs = result.get("brandingSettings", {})
    ch = bs.get("channel", {})
    img = bs.get("image", {})

    if ch.get("description"):
        out.write(f"\n  Description: {ch['description'][:60]}...\n")
    if ch.get("keywords"):
        out.write(f"  Keywords:    {ch['keywords'][:80]}\n")
    if ch.get("country"):
        out.write(f"  Country:     {ch['country']}\n")
    if img.get("bannerExternalUrl"):
        out.write(f"  Banner:      uploaded to YouTube\n")
    if ch.get("unsubscribedTrailer"):
        out.write(f"  Trailer:     {ch['unsubscribedTrailer']}\n")

    # Avatar instructions (always manual)
    out.write("\n" + "-" * 65 + "\n")
    out.write("  MANUAL STEPS (YouTube Studio):\n")
    studio_url = f"https://studio.youtube.com/channel/{channel_id}/editing/details"
    out.write(f"  1. Open: {studio_url}\n")
    out.write("  2. Set channel NAME (can't be changed via API)\n")

    if avatar_path and avatar_path.exists():
        out.write(f"  3. Upload AVATAR: {avatar_path}\n")
        out.write("     (Upload → Profile picture → select the file above)\n")
    else:
        out.write("  3. Upload AVATAR manually\n")

    out.write("=" * 65 + "\n\n")
    out.flush()


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _main() -> None:
    parser = argparse.ArgumentParser(
        description="VideoForge — YouTube Channel Branding Setup (Module 08c)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Generate banner + avatar via AI, then apply all branding
  python modules/08c_channel_setup.py \\
      --channel config/channels/history.json \\
      --channel-name main \\
      --generate

  # Generate only (save to assets/branding/), don't upload
  python modules/08c_channel_setup.py \\
      --channel config/channels/history.json \\
      --channel-name main \\
      --generate --no-upload

  # Use pre-made banner file
  python modules/08c_channel_setup.py \\
      --channel config/channels/history.json \\
      --channel-name main \\
      --banner assets/branding/banner.png

  # Full dry run preview
  python modules/08c_channel_setup.py \\
      --channel config/channels/history.json \\
      --channel-name main \\
      --generate --dry-run
""",
    )
    parser.add_argument("--channel",      required=True,  help="Channel config JSON path")
    parser.add_argument("--channel-name", default=None,
                        help="OAuth2 channel name (e.g. 'main'). Defaults from config.")
    parser.add_argument("--description",  default=None,   help="Channel description text")
    parser.add_argument("--keywords",     default=None,
                        help="Comma-separated keywords (e.g. 'history,ancient rome')")
    parser.add_argument("--country",      default=None,   help="ISO country code (e.g. UA)")
    parser.add_argument("--banner",       default=None,
                        help="Path to pre-made banner PNG/JPG (min 2048x1152). Overrides generation.")
    parser.add_argument("--trailer",      default=None,   help="Video ID for channel trailer")
    parser.add_argument("--generate",     action="store_true",
                        help="Generate banner + avatar via WaveSpeed from branding_prompt in config")
    parser.add_argument("--no-upload",    action="store_true",
                        help="Generate/prepare assets but don't call YouTube API")
    parser.add_argument("--dry-run",      action="store_true",
                        help="Preview all actions without any API calls or file writes")
    args = parser.parse_args()

    keywords_list: list[str] | None = None
    if args.keywords:
        keywords_list = [kw.strip() for kw in args.keywords.split(",") if kw.strip()]

    asyncio.run(setup_channel(
        channel_config_path=args.channel,
        channel_name=args.channel_name,
        description=args.description,
        keywords=keywords_list,
        country=args.country,
        banner_path=args.banner,
        trailer_video_id=args.trailer,
        generate=args.generate,
        no_upload=args.no_upload,
        dry_run=args.dry_run,
    ))


if __name__ == "__main__":
    _main()
