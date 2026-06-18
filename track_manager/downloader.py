"""Main downloader orchestrator."""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from . import __version__
from . import audio as tm_audio
from . import blob as tm_blob
from . import pipeline as tm_pipeline
from .config import Config
from .metadata import sanitize_filename
from .rate_limiter import dab_rate_limit, spotify_rate_limit
from .songlink import SongLinkClient
from .sources import direct, soundcloud, spotify, youtube

_TRACKING_PARAMS = {
    "si",
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
}

# Smart-download dedup/upgrade tuning.
# Source codecs we treat as lossless: once we already own one of these for a
# track, the smart-download path can't do better, so we never re-fetch it.
_LOSSLESS_SOURCE_FORMATS = {"flac", "alac", "aiff", "aif", "wav", "ape", "wv"}

# How many times the smart-download path will auto-attempt to upgrade an owned
# *lossy* copy before giving up. Tracks that these sources only ever serve in
# low quality (e.g. YouTube-only uploads) would otherwise be retried forever.
# Reuses the same per-file counter as `tm upgrade` (provenance.upgrade_attempts).
_SMART_UPGRADE_MAX_ATTEMPTS = 2


def _strip_tracking_params(url: str) -> str:
    """Remove known tracking/optional parameters from a URL before processing."""
    parsed = urlparse(url)
    qs = {k: v for k, v in parse_qs(parsed.query).items() if k not in _TRACKING_PARAMS}
    return urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))


class Downloader:
    """Main downloader class that routes to appropriate source handler."""

    def __init__(
        self,
        config: Config,
        output_dir: Optional[Path] = None,
        dumb: bool = False,
        bypass_cache: bool = False,
    ):
        """Initialize downloader.

        Args:
            config: Configuration object
            output_dir: Override output directory
            dumb: If True, disable smart downloads
            bypass_cache: If True, ignore the persistent TIDAL ISRC→ID cache
                          (forces a fresh song.link lookup for every track).
        """
        self.config = config
        self.output_dir = output_dir or config.output_dir
        self.dumb = dumb
        self.bypass_cache = bypass_cache

        # Ensure output directory exists
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Cached handler for sources whose underlying client can only be
        # initialised once per process (e.g. spotdl's SpotifyClient singleton).
        self._spotify_handler = None
        # Cached DAB Music client (created lazily on first use).
        self._dab_client = None

    def _get_spotify_handler(self):
        """Return (and cache) the SpotifyDownloader, updating its output_dir."""
        from .sources import spotify as spotify_source

        if self._spotify_handler is None:
            self._spotify_handler = spotify_source.SpotifyDownloader(
                self.config, self.output_dir, self
            )
        else:
            # Reuse the existing handler but point it at the current output dir.
            self._spotify_handler.output_dir = self.output_dir
            # Also update spotdl's internal downloader so it writes to the right place.
            self._spotify_handler.spotdl.downloader.settings["output"] = str(
                self.output_dir
            )

        return self._spotify_handler

    def _has_spotify_credentials(self) -> bool:
        """Check if Spotify API credentials are available.

        Returns:
            True if both client_id and client_secret are configured
        """
        import os

        client_id = os.getenv("SPOTIPY_CLIENT_ID", "")
        client_secret = os.getenv("SPOTIPY_CLIENT_SECRET", "")

        if not client_id:
            client_id = self.config.get("spotdl.client_id", "")
        if not client_secret:
            client_secret = self.config.get("spotdl.client_secret", "")

        return bool(client_id and client_secret)

    def _extract_spotify_id(self, url: str) -> Optional[str]:
        """Extract Spotify track ID from URL."""
        import re

        patterns = [
            r"spotify\.com/track/([a-zA-Z0-9]+)",
            r"spotify:track:([a-zA-Z0-9]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        return None

    def _get_isrc_from_spotify(
        self, spotify_id: str, return_metadata: bool = False
    ) -> tuple[Optional[str], Optional[dict]]:
        """Get ISRC from Spotify API.

        Args:
            spotify_id: Spotify track ID
            return_metadata: If True, return (isrc, metadata), else (isrc, None)

        Returns:
            Tuple of (isrc, metadata_dict) if return_metadata=True, else (isrc, None)
        """
        try:
            import spotipy
            from spotipy.oauth2 import SpotifyClientCredentials

            # Get credentials from environment or config
            client_id = os.environ.get("SPOTIPY_CLIENT_ID") or self.config.get(
                "spotdl.client_id"
            )
            client_secret = os.environ.get("SPOTIPY_CLIENT_SECRET") or self.config.get(
                "spotdl.client_secret"
            )

            if not client_id or not client_secret:
                return None, None

            # Initialize Spotify client
            auth_manager = SpotifyClientCredentials(
                client_id=client_id, client_secret=client_secret
            )
            sp = spotipy.Spotify(auth_manager=auth_manager)

            # Get track data with rate limiting
            spotify_rate_limit()
            track = sp.track(spotify_id)
            isrc = track.get("external_ids", {}).get("isrc")

            if return_metadata and isrc:
                # Extract metadata for multi-artist support
                metadata = {
                    "artists": [a["name"] for a in track.get("artists", [])],
                    "title": track.get("name"),
                    "album": track.get("album", {}).get("name"),
                }
                return isrc, metadata

            return isrc, None

        except Exception as e:
            print(f"⚠️ Spotify API error: {e}", file=sys.stderr)
            return None, None

    def _lookup_isrc(
        self, url: str, source_type: str
    ) -> tuple[Optional[str], Optional[dict]]:
        """Lookup ISRC for track URL.

        Returns:
            Tuple of (isrc, spotify_metadata) where spotify_metadata includes artist info
        """
        # Tier 1: Direct from Spotify
        if source_type == "spotify":
            spotify_id = self._extract_spotify_id(url)
            if spotify_id:
                return self._get_isrc_from_spotify(spotify_id, return_metadata=True)

        # Tier 2: Use song.link to find Spotify URL, then get ISRC
        from .songlink import SongLinkClient

        print("🔗 Looking up track on song.link...")
        songlink = SongLinkClient(
            timeout=self.config.songlink_timeout,
            max_retries=self.config.songlink_max_retries,
        )
        spotify_url = songlink.find_spotify_url(url)

        if spotify_url:
            spotify_id = self._extract_spotify_id(spotify_url)
            if spotify_id:
                return self._get_isrc_from_spotify(spotify_id, return_metadata=True)
        else:
            print("ℹ️ No match found on song.link")
            print("   Proceeding to download from original source")

        return None, None

    def _try_qobuz_public(
        self,
        url: str,
        target_format: str,
        spotify_metadata: Optional[dict] = None,
        playlist_url: Optional[str] = None,
        isrc: Optional[str] = None,
    ) -> bool:
        """Try to download from a public Qobuz proxy (lossless FLAC).

        Requires an ISRC — Qobuz lookup is ISRC-only here. When no ISRC is
        available the caller should fall through to TIDAL or YouTube.
        Returns True on success.
        """
        if not isrc:
            return False  # Qobuz requires ISRC; let caller try other sources

        from .qobuz_public import QobuzPublicClient

        try:
            if not hasattr(self, "_qobuz_client"):
                self._qobuz_client = QobuzPublicClient(bypass_cache=self.bypass_cache)
            client = self._qobuz_client

            print(f"🎵 Searching Qobuz (ISRC: {isrc})...")
            temp_path = self.output_dir / f".tmp_qobuz_{isrc}"
            track = client.download_by_isrc(isrc, temp_path)
            if not track:
                print("ℹ️ Track not available via Qobuz")
                return False

            # Probe the bytes — Qobuz consistently returns FLAC, but verify.
            probed = tm_audio.probe_audio(temp_path)
            original_format = probed.get("codec")
            original_bitrate = probed.get("bitrate_kbps")

            doc = self._build_qobuz_doc(
                track,
                isrc,
                spotify_metadata,
                track_url=url,
                playlist_url=playlist_url,
                original_format=original_format,
                original_bitrate=original_bitrate,
            )
            final_path = self._finalize_download(temp_path, doc, target_format)
            if final_path is None:
                print("❌ Qobuz post-processing failed", file=sys.stderr)
                return False

            print(
                f"✅ Downloaded Qobuz ({original_format or 'unknown'}"
                f"{f' {original_bitrate}k' if original_bitrate else ''})"
                f" → {final_path.suffix.upper()[1:]}: {final_path}"
            )
            print()
            return True

        except Exception as e:
            print(f"⚠️ Qobuz error: {e}", file=sys.stderr)
            return False

    def _try_dab_music(
        self,
        isrc: str,
        target_format: str,
        spotify_metadata: Optional[dict] = None,
        track_url: Optional[str] = None,
        playlist_url: Optional[str] = None,
    ) -> bool:
        """Try to download from DAB Music using ISRC.

        Returns True if successful.
        """
        email = self.config.dabmusic_email
        password = self.config.dabmusic_password

        if not email or not password:
            print("ℹ️ DAB Music credentials not configured, skipping")
            return False

        try:
            from .dabmusic import DABMusicClient

            if self._dab_client is None:
                print("🔐 Logging in to DAB Music...")
                self._dab_client = DABMusicClient(
                    email, password, self.config.dabmusic_endpoint
                )

            print("🎵 Searching DAB Music...")
            client = self._dab_client

            track = client.search_by_isrc(isrc)
            if not track:
                print("ℹ️ Track not found on DAB Music")
                return False

            if track.get("isrc") != isrc:
                print(f"⚠️ ISRC mismatch: expected {isrc}, got {track.get('isrc')}")
                return False

            print(f"✅ Found on DAB Music: {track['title']} by {track['artist']}")
            print(f"⬇️ Downloading FLAC from DAB Music...")

            temp_path = self.output_dir / f".tmp_dab_{isrc}.flac"
            ok = client.download_track(track["id"], temp_path, quality=27)
            if not ok:
                print("❌ DAB Music download failed", file=sys.stderr)
                print()
                return False

            doc = self._build_dab_doc(
                track,
                isrc,
                spotify_metadata,
                track_url=track_url or f"isrc:{isrc}",
                playlist_url=playlist_url,
            )
            final_path = self._finalize_download(temp_path, doc, target_format)
            if final_path is None:
                return False

            print(
                f"✅ Downloaded and saved as {final_path.suffix.upper()[1:]}: {final_path}"
            )
            print()
            return True

        except Exception as e:
            print(f"⚠️ DAB Music error: {e}", file=sys.stderr)
            return False

    def _try_tidal_public(
        self,
        url: str,
        target_format: str,
        spotify_metadata: Optional[dict] = None,
        playlist_url: Optional[str] = None,
        isrc: Optional[str] = None,
        prefetched_track: Optional[dict] = None,
    ) -> bool:
        """Try to download from the public TIDAL API.

        Quality tiers (preference order):
          LOSSLESS → FLAC; encoded to `target_format`.
          HIGH     → AAC 320k in MP4; passthrough rename when target=='m4a'
                     else encoded to `target_format`.

        If `prefetched_track` is supplied (e.g. from
        `_resolve_isrc_via_tidal`), the lookup phase is skipped and we go
        straight to the streaming download.
        """
        from .tidal_public import TidalPublicClient

        try:
            if not hasattr(self, "_tidal_client"):
                self._tidal_client = TidalPublicClient(bypass_cache=self.bypass_cache)
            client = self._tidal_client

            if prefetched_track is not None:
                track = prefetched_track
                tidal_id = str(track.get("id"))
            else:
                print("🎵 Looking up track on TIDAL...")
                tidal_id = client.get_tidal_id_from_url(url, isrc=isrc)
                if not tidal_id:
                    print("ℹ️ Track not found on TIDAL")
                    return False

                track = client.get_track_info(tidal_id)
                if not track:
                    print("ℹ️ Could not get track info from TIDAL")
                    return False

                print(
                    f"✅ Found on TIDAL: {track['title']} by {track['artist']['name']}"
                )

            # The TIDAL public endpoint historically promised FLAC for
            # quality=LOSSLESS, but at present it tends to serve AAC HIGH
            # for both tiers. Don't trust the requested quality — write the
            # response to a generic temp file and probe the bytes for truth.
            #
            # Quality fallback (LOSSLESS → HIGH) is handled inside
            # `download_track`, which only retries with HIGH on hosts
            # where the LOSSLESS failure was quality-specific, not
            # auth/network — so dead-TIDAL outages don't double-rotate.
            temp_path = self.output_dir / f".tmp_tidal_{tidal_id}"
            ok = client.download_track(
                tidal_id, temp_path, quality="LOSSLESS", fallback_qualities=("HIGH",)
            )
            if not ok:
                print("❌ TIDAL download failed", file=sys.stderr)
                print()
                return False

            # Probe what we actually got. ffprobe sniffs magic bytes and
            # ignores the extension, so a misnamed AAC blob still reports
            # codec=aac. The audio quality the call asked for is not a
            # reliable signal of what arrived; the bytes are.
            probed = tm_audio.probe_audio(temp_path)
            original_format = probed.get("codec")
            original_bitrate = probed.get("bitrate_kbps")

            doc = self._build_tidal_doc(
                track,
                spotify_metadata,
                track_url=url,
                playlist_url=playlist_url,
                original_format=original_format,
                original_bitrate=original_bitrate,
            )
            final_path = self._finalize_download(temp_path, doc, target_format)
            if final_path is None:
                print("❌ TIDAL post-processing failed", file=sys.stderr)
                return False

            print(
                f"✅ Downloaded TIDAL ({original_format or 'unknown'}"
                f"{f' {original_bitrate}k' if original_bitrate else ''})"
                f" → {final_path.suffix.upper()[1:]}: {final_path}"
            )
            print()
            return True

        except Exception as e:
            print(f"⚠️ TIDAL error: {e}", file=sys.stderr)
            return False

    # ------------------------------------------------------------------
    # Metadata document builders
    # ------------------------------------------------------------------

    def _build_tidal_doc(
        self,
        track: dict,
        spotify_metadata: Optional[dict],
        *,
        track_url: str,
        playlist_url: Optional[str],
        original_format: Optional[str],
        original_bitrate: Optional[int],
    ) -> dict:
        """Construct the canonical metadata document for a TIDAL download.

        `original_format` and `original_bitrate` should come from probing the
        downloaded bytes — the TIDAL quality the call asked for is not a
        reliable signal of what actually arrived.
        """
        doc = tm_blob.empty_document()

        # Display fields (Spotify wins when present; TIDAL fills the gaps).
        if spotify_metadata:
            artists = list(spotify_metadata.get("artists") or [])
            if artists:
                doc["track"]["artists"] = artists
                doc["track"]["artist_string"] = ", ".join(artists)
            doc["track"]["title"] = spotify_metadata.get("title") or track.get("title")
            doc["track"]["album"] = spotify_metadata.get("album") or track.get(
                "album", {}
            ).get("title")
        if not doc["track"]["title"]:
            doc["track"]["title"] = track.get("title")
        if not doc["track"]["artists"]:
            tidal_artists = track.get("artists") or []
            if tidal_artists:
                names = [a["name"] for a in tidal_artists]
                doc["track"]["artists"] = names
                doc["track"]["artist_string"] = ", ".join(names)
            else:
                name = track.get("artist", {}).get("name")
                if name:
                    doc["track"]["artists"] = [name]
                    doc["track"]["artist_string"] = name
        if not doc["track"]["album"]:
            doc["track"]["album"] = track.get("album", {}).get("title")

        if track.get("streamStartDate"):
            doc["track"]["date"] = track["streamStartDate"].split("T")[0]
        if track.get("isrc"):
            doc["track"]["isrc"] = track["isrc"]
        if track.get("trackNumber") is not None:
            doc["track"]["track_number"] = track["trackNumber"]
        if track.get("volumeNumber") is not None:
            doc["track"]["disc_number"] = track["volumeNumber"]
        if track.get("duration") is not None:
            try:
                doc["track"]["duration_seconds"] = float(track["duration"])
            except (TypeError, ValueError):
                pass

        if track.get("id") is not None:
            doc["identifiers"]["tidal_id"] = str(track["id"])

        if track.get("album", {}).get("cover"):
            cover_path = track["album"]["cover"].replace("-", "/")
            doc["cover_art"][
                "url"
            ] = f"https://resources.tidal.com/images/{cover_path}/1280x1280.jpg"

        doc["provenance"]["track_url"] = track_url
        doc["provenance"]["playlist_url"] = playlist_url
        doc["provenance"]["source"] = "tidal-public"
        doc["provenance"]["original_format"] = original_format
        doc["provenance"]["original_bitrate"] = original_bitrate
        doc["provenance"]["downloaded_at"] = datetime.now(timezone.utc).isoformat()
        doc["provenance"]["tool_version"] = __version__

        return doc

    def _build_qobuz_doc(
        self,
        track: dict,
        isrc: str,
        spotify_metadata: Optional[dict],
        *,
        track_url: str,
        playlist_url: Optional[str],
        original_format: Optional[str],
        original_bitrate: Optional[int],
    ) -> dict:
        """Construct the canonical metadata document for a Qobuz download.

        Qobuz returns a rich track payload with nested `performer`,
        `album`, `audio_info`, etc. Spotify metadata still wins for the
        display name (artist/title/album) when present.
        """
        doc = tm_blob.empty_document()

        # Display fields (Spotify wins when present; Qobuz fills the gaps).
        if spotify_metadata:
            artists = list(spotify_metadata.get("artists") or [])
            if artists:
                doc["track"]["artists"] = artists
                doc["track"]["artist_string"] = ", ".join(artists)
            doc["track"]["title"] = spotify_metadata.get("title") or track.get("title")
            doc["track"]["album"] = spotify_metadata.get("album") or (
                track.get("album") or {}
            ).get("title")
        if not doc["track"]["title"]:
            doc["track"]["title"] = track.get("title")
        if not doc["track"]["artists"]:
            performer = (track.get("performer") or {}).get("name")
            if performer:
                doc["track"]["artists"] = [performer]
                doc["track"]["artist_string"] = performer
        if not doc["track"]["album"]:
            doc["track"]["album"] = (track.get("album") or {}).get("title")

        album = track.get("album") or {}
        # Qobuz date is a Unix timestamp at UTC midnight.
        released_at = album.get("released_at")
        if released_at:
            try:
                doc["track"]["date"] = (
                    datetime.fromtimestamp(released_at, tz=timezone.utc)
                    .date()
                    .isoformat()
                )
            except (OSError, ValueError, OverflowError):
                pass
        doc["track"]["isrc"] = isrc
        if (album.get("label") or {}).get("name"):
            doc["track"]["label"] = album["label"]["name"]
        if track.get("track_number") is not None:
            doc["track"]["track_number"] = track["track_number"]
        if track.get("media_number") is not None:
            doc["track"]["disc_number"] = track["media_number"]
        if track.get("duration") is not None:
            try:
                doc["track"]["duration_seconds"] = float(track["duration"])
            except (TypeError, ValueError):
                pass

        if track.get("id") is not None:
            doc["identifiers"]["qobuz_id"] = str(track["id"])
        if album.get("upc"):
            doc["identifiers"]["barcode"] = album["upc"]

        cover = (album.get("image") or {}).get("large") or (
            album.get("image") or {}
        ).get("thumbnail")
        if cover:
            doc["cover_art"]["url"] = cover

        doc["provenance"]["track_url"] = track_url
        doc["provenance"]["playlist_url"] = playlist_url
        doc["provenance"]["source"] = "qobuz-public"
        doc["provenance"]["original_format"] = original_format
        doc["provenance"]["original_bitrate"] = original_bitrate
        doc["provenance"]["downloaded_at"] = datetime.now(timezone.utc).isoformat()
        doc["provenance"]["tool_version"] = __version__

        return doc

    def _build_dab_doc(
        self,
        track: dict,
        isrc: str,
        spotify_metadata: Optional[dict],
        *,
        track_url: str,
        playlist_url: Optional[str],
    ) -> dict:
        """Construct the canonical metadata document for a DAB Music download."""
        doc = tm_blob.empty_document()

        if spotify_metadata:
            artists = list(spotify_metadata.get("artists") or [])
            if artists:
                doc["track"]["artists"] = artists
                doc["track"]["artist_string"] = ", ".join(artists)
            doc["track"]["title"] = spotify_metadata.get("title") or track.get("title")
            doc["track"]["album"] = spotify_metadata.get("album") or track.get(
                "albumTitle"
            )
        if not doc["track"]["title"]:
            doc["track"]["title"] = track.get("title")
        if not doc["track"]["artists"]:
            artist = track.get("artist") or ""
            if artist:
                doc["track"]["artists"] = [artist]
                doc["track"]["artist_string"] = artist
        if not doc["track"]["album"]:
            doc["track"]["album"] = track.get("albumTitle")

        if track.get("releaseDate"):
            doc["track"]["date"] = track["releaseDate"]
        doc["track"]["isrc"] = isrc
        if track.get("label"):
            doc["track"]["label"] = track["label"]
        if track.get("trackNumber") is not None:
            doc["track"]["track_number"] = track["trackNumber"]

        if track.get("upc"):
            doc["identifiers"]["barcode"] = track["upc"]

        if track.get("albumCover"):
            doc["cover_art"]["url"] = track["albumCover"]

        doc["provenance"]["track_url"] = track_url
        doc["provenance"]["playlist_url"] = playlist_url
        doc["provenance"]["source"] = "dab"
        doc["provenance"]["original_format"] = "flac"
        doc["provenance"]["original_bitrate"] = None
        doc["provenance"]["downloaded_at"] = datetime.now(timezone.utc).isoformat()
        doc["provenance"]["tool_version"] = __version__

        return doc

    # ------------------------------------------------------------------
    # Encode + tag + blob pipeline (shared by all smart-download sources)
    # ------------------------------------------------------------------

    def _finalize_download(
        self,
        temp_path: Path,
        doc: dict,
        target_format: str,
        cover_data: Optional[bytes] = None,
    ) -> Optional[Path]:
        """Build a filename from the doc and run the shared finalize pipeline."""
        artist_for_name = doc["track"].get("artist_string") or "Unknown"
        title_for_name = doc["track"].get("title") or temp_path.stem
        final_name = (
            f"{sanitize_filename(artist_for_name)} - "
            f"{sanitize_filename(title_for_name)}.{target_format}"
        )
        final_path = self.output_dir / final_name
        return tm_pipeline.finalize(
            temp_path, final_path, doc, target_format, cover_data
        )

    def detect_source(self, url: str) -> str:
        """Detect source type from URL.

        Args:
            url: URL to analyze

        Returns:
            Source type: 'spotify', 'youtube', 'soundcloud', or 'direct'

        Raises:
            ValueError: If URL is invalid or not supported
        """
        parsed = urlparse(url)

        # Validate URL has proper scheme
        if not parsed.scheme or parsed.scheme not in ["http", "https"]:
            raise ValueError(
                f"Invalid URL: '{url}'\n"
                "URLs must start with http:// or https://\n"
                "Run 'track-manager --help' for usage examples"
            )

        # Validate URL has domain
        if not parsed.netloc:
            raise ValueError(
                f"Invalid URL: '{url}'\n"
                "URL must include a domain name\n"
                "Run 'track-manager --help' for usage examples"
            )

        domain = parsed.netloc.lower()

        if "spotify.com" in domain:
            return "spotify"
        elif "youtube.com" in domain or "youtu.be" in domain:
            return "youtube"
        elif "soundcloud.com" in domain:
            return "soundcloud"
        else:
            # Check if it looks like a direct audio file URL
            parsed_path = parsed.path.lower()
            audio_extensions = [
                ".mp3",
                ".m4a",
                ".flac",
                ".wav",
                ".ogg",
                ".aac",
                ".opus",
                ".wma",
            ]

            if any(parsed_path.endswith(ext) for ext in audio_extensions):
                return "direct"
            else:
                # Unknown platform URL (Apple Music, Deezer, Amazon Music, etc.)
                # Will attempt smart download via song.link → TIDAL
                return "unknown"

    def try_smart_download(
        self,
        url: str,
        format: str,
        isrc: Optional[str] = None,
        spotify_metadata: Optional[dict] = None,
        playlist_url: Optional[str] = None,
        check_duplicates: bool = False,
    ) -> bool:
        """Try to download via the smart-download chain (Qobuz → TIDAL).

        Args:
            url: Track URL (for ISRC lookup if needed)
            format: Output format ('auto'/'aiff'/'m4a'/'mp3'); resolved here.
            isrc: Pre-fetched ISRC (optional)
            spotify_metadata: Pre-fetched Spotify metadata (optional)
            playlist_url: Playlist URL if downloading from a playlist
            check_duplicates: When True, look for an existing copy in the
                library *before* downloading. If we already own a lossless
                copy the configured ``duplicates.handling`` mode applies; if we
                own only a lossy copy, an in-place upgrade is attempted (capped
                by ``_SMART_UPGRADE_MAX_ATTEMPTS``). Callers that run their own
                pre-download dedup (e.g. SpotifyDownloader) leave this False to
                avoid double-checking.

        Returns:
            True if downloaded successfully, False if caller should fall back.
        """
        if self.dumb:
            return False

        target_format = tm_audio.resolve_format(format)

        if isrc:
            print(f"🔍 Using ISRC from Spotify: {isrc}")

        # Skip smart download for direct audio URLs.
        source_type = self.detect_source(url)
        if source_type == "direct":
            return False

        # Resolve once: if we don't already know the ISRC (typical for
        # SoundCloud/YouTube URLs), look it up via TIDAL — song.link gives us
        # a TIDAL track id from the URL, and TIDAL's /info/ endpoint gives us
        # the ISRC. The api pool is much more reliable than the streaming
        # pool, so this works even when TIDAL streaming hosts are all 502.
        # Both branches below reuse the prefetched track_info to avoid a
        # second round-trip.
        tidal_track = None
        if not isrc:
            isrc, tidal_track = self._resolve_isrc_via_tidal(url)

        # Quality-aware dedup: now that the ISRC is resolved (the strongest
        # cross-source identity), see if we already own this track and can
        # skip the download or turn it into an in-place upgrade instead.
        # This only ever runs on the smart-download path — `--dumb` (self.dumb)
        # returns above, so the in-place upgrade never fires for direct/dumb
        # downloads.
        if check_duplicates:
            handled = self._dedup_or_upgrade(url, isrc, spotify_metadata)
            if handled:
                return True

        # Smart-download chain (lossless first):
        #   1) Qobuz public proxy — true 16/44.1 FLAC, requires ISRC.
        #      Fast and reliable as long as kennyy.com.br is up.
        #   2) TIDAL public hifi-api — LOSSLESS FLAC (or HIGH AAC) via
        #      community-hosted streaming proxies. Volatile.
        # Falls through to YouTube/SoundCloud/spotdl in the caller when both fail.
        if self._try_qobuz_public(
            url,
            target_format,
            spotify_metadata,
            playlist_url=playlist_url,
            isrc=isrc,
        ):
            return True
        return self._try_tidal_public(
            url,
            target_format,
            spotify_metadata,
            playlist_url=playlist_url,
            isrc=isrc,
            prefetched_track=tidal_track,
        )

    def _find_owned_copy(self, url: str, isrc: Optional[str]) -> Optional[Path]:
        """Return an existing library file for this track, or None.

        Uses strong identity only — ISRC first, then the stored TRACK_URL — so
        an in-place upgrade can never replace a *different* recording (e.g. a
        live or remix variant) that merely shares an artist/title.
        """
        from .duplicates import find_duplicates_by_isrc, find_duplicates_by_track_url

        if isrc:
            matches = find_duplicates_by_isrc(isrc, self.output_dir)
            if matches:
                return matches[0]
        if url:
            matches = find_duplicates_by_track_url(url, self.output_dir)
            if matches:
                return matches[0]
        return None

    @staticmethod
    def _is_lossless_source(source_quality: dict) -> bool:
        """True if the owned file's *source* codec is lossless."""
        fmt = (source_quality.get("format") or "").lower()
        if not fmt:
            return False
        return fmt.startswith("pcm") or fmt in _LOSSLESS_SOURCE_FORMATS

    @staticmethod
    def _describe_source(source_quality: dict) -> str:
        """Human-readable 'aac 128k' / 'flac' summary for messaging."""
        fmt = source_quality.get("format") or "unknown"
        bitrate = source_quality.get("bitrate_kbps")
        return f"{fmt} {bitrate}k" if bitrate else str(fmt)

    def _dedup_or_upgrade(
        self,
        url: str,
        isrc: Optional[str],
        spotify_metadata: Optional[dict],
    ) -> Optional[bool]:
        """Quality-aware pre-download dedup for the smart-download path.

        Looks for an existing copy of the track (by ISRC, then TRACK_URL)
        before spending a download:

          * Already own a *lossless* copy → defer to ``duplicates.handling``
            (skip / keep / interactive).
          * Own only a *lossy* copy → attempt an in-place upgrade, capped at
            ``_SMART_UPGRADE_MAX_ATTEMPTS`` so tracks these sources only ever
            serve in low quality aren't retried forever.
          * Don't own it → return None so the caller downloads normally.

        Returns:
            True if handled (existing kept, or upgraded in place); the caller
            should treat the smart download as done. None to proceed with a
            fresh download.
        """
        from . import duplicates as tm_dup
        from . import upgrade as tm_upgrade

        owned = self._find_owned_copy(url, isrc)
        if owned is None:
            return None

        src = tm_upgrade._source_quality(owned)
        artist, title = tm_dup.extract_metadata(owned)

        if self._is_lossless_source(src):
            # Already lossless — the smart path can't improve on it, so this is
            # a pure duplicate. Defer the decision to the configured mode.
            skip = tm_dup.handle_duplicates(
                [owned],
                self.config.duplicate_handling,
                artist=artist,
                title=title,
            )
            return True if skip else None

        attempts = tm_upgrade._read_upgrade_attempts(owned)
        if attempts >= _SMART_UPGRADE_MAX_ATTEMPTS:
            print(
                f"⏭️ Skipped: already own {self._describe_source(src)} and "
                f"{attempts} upgrade attempt(s) already made → {owned.name}"
            )
            return True

        # Own a lossy copy under the attempt cap → try to upgrade it in place
        # (preserves filename so Rekordbox cue points survive). upgrade_track
        # bumps the attempt counter and only replaces if the new file is
        # genuinely better, so a failed attempt still counts toward the cap.
        print(
            f"⬆️ Already own a lossy copy ({self._describe_source(src)}); "
            f"attempting upgrade → {owned.name}"
        )
        saved_output_dir = self.output_dir
        try:
            ok, msg = tm_upgrade.upgrade_track(owned, url, self.config, downloader=self)
        finally:
            # upgrade_track points us at a temp dir for the re-download; restore
            # the real library dir for any subsequent work on this Downloader.
            self.output_dir = saved_output_dir

        if ok:
            print(f"⬆️ Upgraded: {msg}")
        else:
            print(f"⏭️ Kept existing copy ({msg})")
        return True

    def _resolve_isrc_via_tidal(self, url: str) -> tuple[Optional[str], Optional[dict]]:
        """Discover a track's ISRC by looking it up on TIDAL via song.link.

        Returns `(isrc, track_info)`. Either may be None if the lookup
        failed (track not on TIDAL, all api endpoints down, etc.). The
        `track_info` is returned so callers can hand it back to
        `_try_tidal_public` and skip the redundant fetch.
        """
        try:
            from .tidal_public import TidalPublicClient

            if not hasattr(self, "_tidal_client"):
                self._tidal_client = TidalPublicClient(bypass_cache=self.bypass_cache)
            client = self._tidal_client

            print("🔍 Resolving ISRC via TIDAL...")
            tidal_id = client.get_tidal_id_from_url(url)
            if not tidal_id:
                print("ℹ️ Track not found on TIDAL")
                return None, None

            track = client.get_track_info(tidal_id)
            if not track:
                print("ℹ️ Could not get track info from TIDAL")
                return None, None

            isrc = track.get("isrc")
            artist = track.get("artist", {}).get("name") or "?"
            isrc_suffix = f" (ISRC: {isrc})" if isrc else ""
            print(f"✅ Matched on TIDAL: {track.get('title')} by {artist}{isrc_suffix}")
            return isrc, track
        except Exception as e:
            print(f"⚠️ TIDAL lookup error: {e}", file=sys.stderr)
            return None, None

    def download(
        self, url: str, format: str = "auto", show_header: bool = True
    ) -> Optional[bool]:
        """Download track(s) from URL.

        Args:
            url: URL to download from
            format: Output format ('auto', 'aiff', 'm4a', 'mp3')
            show_header: Print source/output-directory header lines.  Set to
                False when the caller (e.g. upgrade) manages its own context.
        """
        url = _strip_tracking_params(url)
        source_type = self.detect_source(url)
        target_format = tm_audio.resolve_format(format)

        # Fail-fast when FFmpeg/ffprobe are missing to avoid downloading large
        # files (lossless FLAC) only to fail during the encode/probe step.
        # Use the shared dependency helper so messages are consistent and testable
        from . import deps as tm_deps

        try:
            tm_deps.ensure_ffmpeg_available()
        except tm_deps.MissingDependencyError as e:
            # Reraise as RuntimeError so callers (CLI) treat it the same as other
            # preflight failures we already surface.
            raise RuntimeError(str(e))

        if show_header:
            print(f"🎵 Detected source: {source_type.title()}")
            print(f"📁 Output directory: {self.output_dir}")
            print(f"🎚️  Target format: {target_format.upper()}")
            print()

        # Route to appropriate handler (handlers now manage smart downloads internally)
        if source_type == "spotify":
            # Check if Spotify credentials are available
            if not self._has_spotify_credentials():
                # Determine if it's a playlist/album or single track
                is_playlist = "/playlist/" in url or "/album/" in url

                if is_playlist:
                    print(
                        "❌ Spotify playlists/albums require API credentials",
                        file=sys.stderr,
                    )
                    print(
                        "\n📝 Spotify API credentials are optional but needed for playlists:",
                        file=sys.stderr,
                    )
                    print(
                        "   • Individual Spotify tracks work without credentials (via TIDAL)",
                        file=sys.stderr,
                    )
                    print(
                        "   • Playlists/albums require Spotify API setup\n",
                        file=sys.stderr,
                    )
                    print("🔧 To enable Spotify playlist support:", file=sys.stderr)
                    print(
                        "   1. Get credentials from: https://developer.spotify.com/dashboard",
                        file=sys.stderr,
                    )
                    print("   2. Set environment variables:", file=sys.stderr)
                    print("      export SPOTIPY_CLIENT_ID='your_id'", file=sys.stderr)
                    print(
                        "      export SPOTIPY_CLIENT_SECRET='your_secret'",
                        file=sys.stderr,
                    )
                    print("   3. Or add to config.yaml:", file=sys.stderr)
                    print("      spotdl:", file=sys.stderr)
                    print("        client_id: 'your_id'", file=sys.stderr)
                    print("        client_secret: 'your_secret'\n", file=sys.stderr)
                    self._log_failure(url, "Spotify playlist requires API credentials")
                    return False
                else:
                    # Single track - download via TIDAL (song.link)
                    print("ℹ️ Spotify API not configured, downloading via TIDAL")
                    print("   (For playlist support, add Spotify API credentials)")
                    print()

                    # Try smart download directly (bypasses Spotify handler)
                    success = self.try_smart_download(
                        url, format, check_duplicates=True
                    )
                    if success:
                        return True
                    else:
                        print("❌ Failed to download via TIDAL", file=sys.stderr)
                        print(
                            "   Spotify API credentials needed for this track",
                            file=sys.stderr,
                        )
                        self._log_failure(
                            url, "TIDAL download failed, Spotify API needed"
                        )
                        return False

            # If we get here, we have Spotify credentials
            handler = self._get_spotify_handler()
        elif source_type == "youtube":
            handler = youtube.YouTubeDownloader(self.config, self.output_dir, self)
        elif source_type == "soundcloud":
            handler = soundcloud.SoundCloudDownloader(
                self.config, self.output_dir, self
            )
        elif source_type == "unknown":
            # Unrecognized platform (Apple Music, Deezer, etc.)
            # Try smart download via song.link → TIDAL
            print("🔍 Unknown platform, attempting smart download via TIDAL...")
            print()
            success = self.try_smart_download(url, format, check_duplicates=True)
            if success:
                return True  # Success, we're done
            else:
                print(
                    "❌ Platform not recognized and not found on TIDAL", file=sys.stderr
                )
                print(
                    "   Supported: Spotify, YouTube, SoundCloud, or direct audio URLs",
                    file=sys.stderr,
                )
                self._log_failure(url, "Unknown platform, not available via TIDAL")
                return False  # Don't create garbage files
        elif source_type == "direct":
            # Only for confirmed direct audio file URLs
            handler = direct.DirectDownloader(self.config, self.output_dir)
        else:
            # Should never reach here, but handle gracefully
            print(f"❌ Unsupported source type: {source_type}", file=sys.stderr)
            self._log_failure(url, f"Unsupported source type: {source_type}")
            return False

        # Download
        try:
            return handler.download(url, format)
        except Exception as e:
            print(f"❌ Download failed: {e}", file=sys.stderr)
            # Log to failed downloads
            self._log_failure(url, str(e))
            raise

    def _log_failure(self, url: str, error: str):
        """Log failed download.

        Args:
            url: URL that failed
            error: Error message
        """
        from datetime import datetime

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        log_entry = f"{timestamp} | {url} | {error}\n"

        with open(self.config.failed_log, "a") as f:
            f.write(log_entry)
