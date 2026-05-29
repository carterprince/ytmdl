#!/usr/bin/env python3

import os
import queue
import threading
import time
from pathlib import Path

# Third-party dependencies
import yt_dlp
from rich.live import Live
from rich.console import Group
from rich.text import Text

# Configuration
PLAYLIST_FILE = Path("~/Music/playlist.txt").expanduser()
OUTPUT_DIR = PLAYLIST_FILE.parent
NUM_WORKERS = 8
BAR_WIDTH = 40  # Width of the visual progress bar

# Global State
worker_states = ["idle"] * NUM_WORKERS
downloaded_count = 0
total_urls = 0
lock = threading.Lock()

def sanitize_filename(name):
    """Remove invalid characters for cross-platform file paths."""
    return "".join(c for c in name if c not in r'\/:*?"<>|')

def worker_thread(worker_id, url_queue):
    global downloaded_count
    
    # Create ONE instance per worker and keep it open. 
    # This preserves session cookies/tokens and prevents 403 Forbidden errors.
    dl_opts = {
        'format': 'bestaudio/best',
        # We will inject 'safe_artist' and 'safe_title' into the info dict dynamically
        'outtmpl': str(OUTPUT_DIR / '%(safe_artist)s - %(safe_title)s.%(ext)s'),
        'postprocessors': [
            {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'},
            {'key': 'EmbedThumbnail'},
            {'key': 'FFmpegMetadata'},
        ],
        'writethumbnail': True,
        'quiet': True,
        'no_warnings': True,
        'noprogress': True,
        'noplaylist': True,
        'ignoreerrors': True,
    }
    
    with yt_dlp.YoutubeDL(dl_opts) as ydl:
        while True:
            try:
                url = url_queue.get_nowait()
            except queue.Empty:
                worker_states[worker_id] = "done"
                break
                
            worker_states[worker_id] = "pending"
            
            try:
                # STEP 1: Fetch metadata using this instance
                info = ydl.extract_info(url, download=False)
                
                if not info:
                    worker_states[worker_id] = "error (unavailable)"
                    continue
                    
                if info.get('_type') == 'playlist':
                    entries = info.get('entries', [])
                    if not entries:
                        worker_states[worker_id] = "error (empty)"
                        continue
                    info = entries[0]
                    
                # Clean up metadata
                artist = info.get('artist') or info.get('uploader') or info.get('channel') or 'Unknown'
                if artist.endswith(' - Topic'):
                    artist = artist[:-8]
                title = info.get('title') or 'Unknown'
                
                safe_artist = sanitize_filename(artist)
                safe_title = sanitize_filename(title)
                
                # Inject cleaned metadata back into info dict for FFmpegMetadata to embed
                info['artist'] = artist
                info['uploader'] = artist
                info['title'] = title
                
                # Inject safe strings for outtmpl to use for the file path
                info['safe_artist'] = safe_artist
                info['safe_title'] = safe_title
                
                # Skip if already downloaded
                expected_filename = f"{safe_artist} - {safe_title}.mp3"
                if (OUTPUT_DIR / expected_filename).exists():
                    worker_states[worker_id] = "skipped"
                    continue
                    
                # Update UI to show downloading title
                worker_states[worker_id] = f"{safe_artist} - {safe_title}"
                
                # STEP 2: Proceed with download using the SAME instance and modified info
                ydl.process_ie_result(info, download=True)
                
            except Exception:
                worker_states[worker_id] = "error"
            finally:
                with lock:
                    downloaded_count += 1
                url_queue.task_done()

def generate_ui():
    """Generates the Rich UI layout with a static progress bar."""
    # Calculate progress bar metrics
    percent = downloaded_count / total_urls if total_urls > 0 else 0
    filled_len = int(BAR_WIDTH * percent)
    bar_str = ("█" * filled_len) + ("░" * (BAR_WIDTH - filled_len))
    
    # Calculate fixed widths to prevent jitter
    max_digits = len(str(total_urls))
    count_width = (max_digits * 2) + 1  # e.g., "10/10" is 5 chars
    
    count_str = f"{downloaded_count}/{total_urls}"
    percent_str = f"({int(percent * 100)}%)"
    
    # Assemble the header (using right-alignment padding for the stats)
    header_text = f"{bar_str}  {count_str:>{count_width}} {percent_str:>6}"
    lines = [Text(header_text, style="bold green")]
    
    # Append worker states
    for i, state in enumerate(worker_states):
        if state == "pending":
            style = "yellow"
        elif state in ("idle", "done"):
            style = "dim"
        elif state == "skipped":
            style = "blue"
        elif state.startswith("error"):
            style = "red"
        else:
            style = "cyan"
            
        lines.append(Text(f"Worker {i+1}: {state}", style=style, no_wrap=True, overflow="ellipsis"))
        
    return Group(*lines)

def main():
    global total_urls
    
    if not PLAYLIST_FILE.exists():
        print(f"Playlist not found: {PLAYLIST_FILE}")
        return
        
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        
    with open(PLAYLIST_FILE, "r", encoding="utf-8") as f:
        urls = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        
    total_urls = len(urls)
    if total_urls == 0:
        print("No URLs found in playlist.")
        return
        
    # Queue up the URLs
    url_queue = queue.Queue()
    for url in urls:
        url_queue.put(url)
        
    # Spawn workers
    threads = []
    for i in range(NUM_WORKERS):
        t = threading.Thread(target=worker_thread, args=(i, url_queue))
        t.daemon = True
        t.start()
        threads.append(t)
        
    # Run Live UI
    with Live(generate_ui(), refresh_per_second=10) as live:
        while any(t.is_alive() for t in threads):
            live.update(generate_ui())
            time.sleep(0.1)
        # Final update
        live.update(generate_ui())

if __name__ == "__main__":
    main()
