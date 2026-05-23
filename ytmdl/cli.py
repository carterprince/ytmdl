"""
Download a YouTube playlist as MP3s with video-frame thumbnails.
"""

import atexit, json, os, shutil, subprocess, sys, tempfile, threading
from queue import Queue, Empty
from rich.console import Console
from rich.progress import (
    Progress, SpinnerColumn, TextColumn, BarColumn,
    MofNCompleteColumn, TimeElapsedColumn,
)

TMPL = "%(artist&{} - |)s%(track,title)s"
STEPS = 4


def restore_terminal():
    sys.stdout.write("\033[?25h\033[0m")
    sys.stdout.flush()
    os.system("stty sane 2>/dev/null")

atexit.register(restore_terminal)


def playlist_entries(url):
    r = subprocess.run(
        ["yt-dlp", "--yes-playlist", "--flat-playlist", "-J", url],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(r.stdout)

    if "entries" not in data:
        return [
            {
                "url": data.get("webpage_url", url),
                "title": data.get("title", data["id"]),
                "id": data["id"],
            }
        ]

    return [
        {
            "url": f"https://www.youtube.com/watch?v={e['id']}",
            "title": e.get("title", e["id"]),
            "id": e["id"],
        }
        for e in data["entries"]
    ]


def process(entry, tmpdir, prog, task, overall):
    url, vid = entry["url"], entry["id"]
    label = entry["title"][:42]

    # 1 ── resolve desired output name
    prog.update(task, completed=0, description=f"[dim]… resolve[/] {label}")
    stem = subprocess.run(
        ["yt-dlp", "--print", TMPL, url],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    mp3 = f"{stem}.mp3"
    prog.advance(task)

    if os.path.exists(mp3):
        prog.update(task, completed=STEPS, description=f"[dim]✓ skip   [/] {label}")
        prog.advance(overall)
        return

    # 2 ── download audio into tmpdir (vid ID as filename — no mismatch)
    prog.update(task, description=f"[cyan]↓ audio [/] {label}")
    subprocess.run(
        ["yt-dlp", "-x", "--audio-format", "mp3", "--audio-quality", "0",
         "--embed-metadata", "--no-progress",
         "-o", os.path.join(tmpdir, f"{vid}.%(ext)s"), url],
        capture_output=True, check=True,
    )
    src = os.path.join(tmpdir, f"{vid}.mp3")
    prog.advance(task)

    # 3 ── grab one frame (non-fatal if it fails)
    prog.update(task, description=f"[yellow]⎔ frame [/] {label}")
    thumb = os.path.join(tmpdir, f"{vid}.jpg")
    got_frame = False
    try:
        stream = subprocess.run(
            ["yt-dlp", "-f", "bv[height>=720]/bv/b", "--get-url", url],
            capture_output=True, text=True, check=True,
        ).stdout.strip().split("\n")[0]
        subprocess.run(
            ["ffmpeg", "-y", "-ss", "0.5", "-i", stream,
             "-frames:v", "1", "-q:v", "2", thumb],
            capture_output=True, check=True,
        )
        got_frame = True
    except subprocess.CalledProcessError:
        prog.console.print(f"[yellow]⚠ Worker — {label}: frame grab failed, skipping cover[/yellow]")
    prog.advance(task)

    # 4 ── embed cover if we got a frame, otherwise just move
    prog.update(task, description=f"[green]⊕ embed [/] {label}")
    if got_frame:
        dst = os.path.join(tmpdir, f"{vid}_cover.mp3")
        subprocess.run(
            ["ffmpeg", "-y", "-i", src, "-i", thumb,
             "-map", "0:a", "-map", "1:0", "-c", "copy",
             "-id3v2_version", "3",
             "-metadata:s:v", "title=Album cover",
             "-metadata:s:v", "comment=Cover (front)",
             dst],
            capture_output=True, check=True,
        )
        shutil.move(dst, mp3)
        os.remove(src)
        os.remove(thumb)
    else:
        shutil.move(src, mp3)
    prog.advance(task)

    prog.advance(overall)


def worker(wid, q, tmpdir, prog, task, overall):
    while True:
        try:
            entry = q.get_nowait()
        except Empty:
            break
        try:
            process(entry, tmpdir, prog, task, overall)
        except subprocess.CalledProcessError as exc:
            err = exc.stderr
            if isinstance(err, bytes):
                err = err.decode(errors="replace")
            tail = "\n".join(err.strip().splitlines()[-5:]) if err else str(exc)
            prog.console.print(
                f"\n[red bold]✗ Worker {wid} — {entry['title']}[/red bold]\n{tail}\n"
            )
            prog.advance(overall)
        except Exception as exc:
            prog.console.print(
                f"\n[red bold]✗ Worker {wid} — {entry['title']}[/red bold]\n{exc}\n"
            )
            prog.advance(overall)
        finally:
            q.task_done()
    prog.update(task, description=f"[dim]Worker {wid} — done[/dim]", completed=STEPS)


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print("Usage: ytmdl <PLAYLIST_URL> [WORKERS]")
        sys.exit(0)

    url = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 8

    con = Console()
    con.print("[bold]Fetching playlist…[/bold]")
    entries = playlist_entries(url)
    con.print(f"[bold]{len(entries)} videos · {n} workers\n[/bold]")

    tmpdir = tempfile.mkdtemp(prefix="ytmdl-")
    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(bar_width=24),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=con,
        ) as prog:
            overall = prog.add_task("[bold blue]Overall", total=len(entries))
            tasks = [
                prog.add_task(f"[dim]Worker {i} — waiting[/dim]", total=STEPS)
                for i in range(n)
            ]

            q = Queue()
            for e in entries:
                q.put(e)

            threads = [
                threading.Thread(
                    target=worker,
                    args=(i, q, tmpdir, prog, tasks[i], overall),
                    daemon=True,
                )
                for i in range(n)
            ]
            for t in threads:
                t.start()
            while any(t.is_alive() for t in threads):
                for t in threads:
                    t.join(timeout=0.5)
    except KeyboardInterrupt:
        restore_terminal()
        print("\nInterrupted.")
        sys.exit(130)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    con.print(f"\n[bold green]✓ {len(entries)} tracks done[/bold green]")


if __name__ == "__main__":
    main()
