# Track Manager

Universal music downloader with smart duplicate detection and metadata management.

## Features

- 🎯 **Universal Platform Support** - Works with ANY music platform (Spotify, Apple Music, YouTube, SoundCloud, Deezer, Amazon Music, TIDAL, etc.)
- 🎵 **High-Quality Downloads** - Automatic FLAC from TIDAL proxy
- 🔍 **Smart Duplicate Detection** - Works across formats (M4A vs MP3)
- 📝 **Metadata Management** - CSV-based review and correction workflow
- 🤝 **Interactive Prompts** - Asks what to do when duplicates found
- 📊 **Playlist Support** - YouTube, SoundCloud, and Spotify playlists (Spotify requires API credentials)
- 🔄 **Error Resilience** - Logs failed downloads, continues on errors
- 🎚️ **Best Quality** - Lossless FLAC (16-bit/44.1kHz) when available, converted to M4A 256kbps
- 🌍 **Cross-Platform** - Works on macOS, Linux, Windows

## How It Works

Track Manager uses a **smart download system** to get the best quality audio:

1. **Any URL** (Spotify, YouTube, etc.) → song.link API lookup
2. **TIDAL proxy API** → Downloads lossless FLAC (16-bit/44.1kHz)
   - ✅ No credentials required
   - ✅ Includes full metadata and cover art
3. **Automatic conversion** → M4A 256kbps AAC (preserves quality, better compatibility)
4. **Fallback** → If not on TIDAL, downloads from from youtube or soundcloud

**Quality comparison:**

- TIDAL FLAC: 1411 kbps lossless → converted to M4A 256 kbps
- YouTube: ~128 kbps M4A
- SoundCloud: ~128 kbps → M4A 256 kbps (for less lossy conversion)

**Legacy note:** DAB Music was previously used for high-quality downloads but is currently unavailable. The configuration is kept for potential future use if the service returns.

## Installation

### From Source (For Development)

```bash
# Navigate to the track-manager directory
cd track-manager

# Install the package
pip install -e .
# or
pip3 install -e .
```

## Setup

### FFmpeg (required)

Track Manager requires FFmpeg (the `ffmpeg` and `ffprobe` binaries) to be installed and available on your PATH. FFmpeg is used to probe audio, perform format conversions (e.g. FLAC → AIFF/M4A/MP3), and embed cover art. Without FFmpeg, lossless downloads and post-download processing will fail with an error such as "ffmpeg not found on PATH".

Install:

Check out the [FFmpeg installation guide](https://www.ffmpeg.org/download.html) for your platform. Alternatively, use your platform's package manager (e.g. Homebrew, apt, dnf) to install FFmpeg.

- macOS (Homebrew):

  ```bash
  brew install ffmpeg
  ```

- Debian/Ubuntu:

  ```bash
  sudo apt-get update && sudo apt-get install -y ffmpeg
  ```

- Fedora:

  ```bash
  sudo dnf install ffmpeg
  ```

- Windows:

  Download a build from https://ffmpeg.org/download.html and add `ffmpeg.exe` to your PATH, or use Chocolatey:

  ```powershell
  choco install ffmpeg
  ```

Verify the installation:

```bash
ffmpeg -version
ffprobe -version
```

### Basic Setup (No Credentials Required)

**Individual tracks** work for ANY platform (Apple Music, YouTube, SoundCloud, Deezer, Amazon Music, TIDAL, etc.):

- ✅ Converted via song.link → TIDAL for high-quality FLAC
- ✅ No credentials needed

**Playlists** only work for:

- ✅ **YouTube playlists** - No setup needed
- ✅ **SoundCloud playlists** - No setup needed
- ⚠️ **Spotify playlists** - Requires API credentials (see below)

### Spotify API Setup (Optional - Only for Playlists)

**Spotify API credentials are optional:**

- ✅ **Individual Spotify track URLs work without credentials** (downloaded via TIDAL)
- ⚠️ **Playlist/album URLs require Spotify API** to enumerate tracks

**To enable Spotify playlist support:**

1. Copy `config.example.yaml` to `config.yaml`

2. Get credentials from: https://developer.spotify.com/dashboard
   (Create an app → Copy Client ID & Secret)

3. add to `config.yaml`:
   ```yaml
   spotdl:
     client_id: "your_client_id"
     client_secret: "your_client_secret"
   ```

## Configuration

Track Manager uses a config file at `config.yaml` in the project root.

You can customize:

- Output directory
- Download format preferences (M4A, MP3)
- Duplicate handling behavior
- Spotify credentials (optional)
- And more...

See `config.example.yaml` for all available options.

## Quick Start

### Download Tracks

```bash
# Download from Spotify
track-manager download "https://open.spotify.com/track/..."
```

or just

```bash
tm "https://open.spotify.com/track/..."
```

### Manage Your Library

```bash
# Check for duplicate tracks
track-manager check-duplicates

# Verify installation and setup
track-manager check-setup

# Get help
track-manager --help
```

## Audio Quality

Track Manager always downloads at the **best available quality** - no configuration needed.
Some download sources like spotdl will encode at higher bit rate that source in order to prevent loss.
In order for you to keep track of the real quality of your tracks, Track Manager add the true bit rate to the metadata.

```bash
# list quality of all tracks grouped into high, medium and low
tm check-quality
```

## Duplicate Detection

Track Manager intelligently detects duplicates by:

- Comparing artist + title from ID3/M4A tags (not filenames)
- Normalizing metadata (removes "[Official Video]", handles "feat." variations)
- Working across formats (finds M4A duplicates of MP3 files)
- Case-insensitive matching

When a duplicate is found, you'll be prompted to:

- Skip new file (keep existing)
- Keep both files
- Replace existing with new file

## Metadata Management

When metadata is missing or problematic, tracks are flagged for manual review:

1. Download script flags tracks with issues
2. Edit `tracks-metadata-review.csv` (in project directory) to fill in correct metadata
3. Run `track-manager apply-metadata` to update files

## Error Handling

Failed downloads are logged to `failed-downloads.txt` with timestamps and error messages. You can retry failed URLs later.

## Troubleshooting

### Spotify Downloads

**Problem:** "Error: No Spotify credentials found"

**Solution:** Spotify playlist downloads require API credentials. See the [Spotify Setup](#spotify-setup-optional) section above for detailed instructions.

Get credentials from: https://developer.spotify.com/dashboard

### Low Quality Downloads

**Problem:** Old tracks downloaded at 128 kbps or lower

**Solution:** The quality fix was implemented in version 0.2.0. If you have old low-quality tracks:

1. Check library quality: Look for tracks < 128 kbps using your audio player's metadata view
2. Re-download those tracks - they'll now download at best quality
3. Remove old low-quality versions

### YouTube Download Issues

**Problem:** "Error: Unable to extract video info"

**Possible causes:**

- Video is private or removed
- Video is age-restricted
- Geo-restricted content
- YouTube rate limiting

**Solution:**

- Verify the URL is correct and accessible in a browser
- Wait a few minutes and retry (rate limiting)
- Check `failed-downloads.txt` for specific error messages

### SoundCloud Issues

**Problem:** Downloads fail or get low quality

**Solution:**

- SoundCloud requires the track to be publicly accessible
- Private tracks or sets cannot be downloaded
- Some tracks may have download disabled by the artist

### Metadata Issues

**Problem:** Tracks have incorrect or missing metadata

**Solution:**

1. Check `tracks-metadata-review.csv` in the track-manager directory
2. Fill in correct artist and title for flagged tracks
3. Run: `track-manager apply-metadata`

### Duplicate Detection

**Problem:** Duplicate detection not working

**Possible causes:**

- Files have no metadata (artist/title tags missing)
- Metadata is very different between files

**Solution:**

- Ensure files have proper ID3/M4A tags
- Use `track-manager apply-metadata` to fix metadata first
- Duplicate detection compares artist + title from tags, not filenames

### Installation Issues

**Problem:** "Command not found: track-manager"

**Solution:**

```bash
# Ensure installation directory is in PATH
pip show track-manager  # Check installation location

# Or use as module
python -m track_manager download <url>
```

**Problem:** Missing dependencies

**Solution:**

```bash
# Run setup check
track-manager check-setup

# Install missing dependencies
pip install track-manager[dev]  # Includes all optional deps
```

### Getting Help

If you encounter other issues:

1. Check `failed-downloads.txt` for error details
2. Run `track-manager check-setup` to verify installation
3. Check the documentation for known issues
4. Open a new issue with:
   - Command you ran
   - Full error message
   - Output of `track-manager check-setup`

## Development

For development, install with dev dependencies. It is recommended to use a virtual environment as a developer:

```bash
cd track-manager
pip install -e ".[dev]"

# Install pre-commit hooks
pre-commit install

# Run tests
pytest

# Run with coverage
pytest --cov=track_manager
```

## License

MIT License - see [LICENSE](LICENSE) for details.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.
