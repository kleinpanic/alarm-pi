"""
Standalone Tkinter alarm popup.

Runs as its own process (`python -m alarm.popup ...`) spawned by the daemon
when a display is available. Its STOP / SNOOZE buttons POST to the daemon's
local HTTP API (127.0.0.1, which bypasses the PIN), so all dismiss/snooze
logic lives in one place. The daemon kills this process if the alarm is
dismissed remotely from the web UI.
"""

import argparse
import urllib.request
from datetime import datetime


def _post(port: int, path: str) -> None:
    """Best-effort POST to the local daemon API."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        pass  # daemon will reconcile; nothing useful to do in the popup


def run_popup(port, label, time_str, snoozable, snooze_minutes, irritable):
    import tkinter as tk

    root = tk.Tk()
    root.title("⏰ ALARM")
    root.attributes("-topmost", True)
    root.configure(bg="#2d2d2d")

    main = tk.Frame(root, bg="#2d2d2d", padx=30, pady=20)
    main.pack(expand=True, fill="both")

    tk.Label(main, text="⏰ ALARM!", font=("Helvetica", 24, "bold"),
             fg="#ff6b6b", bg="#2d2d2d").pack(pady=(0, 15))
    tk.Label(main, text=label, font=("Helvetica", 18), fg="#ffffff",
             bg="#2d2d2d", wraplength=350).pack(pady=(0, 10))
    if time_str:
        tk.Label(main, text=f"Scheduled: {time_str}", font=("Helvetica", 12),
                 fg="#aaaaaa", bg="#2d2d2d").pack()

    clock = tk.Label(main, text="", font=("Helvetica", 14), fg="#ffffff", bg="#2d2d2d")
    clock.pack(pady=(5, 20))

    def tick():
        try:
            clock.config(text=f"Current time: {datetime.now().strftime('%H:%M:%S')}")
            root.after(1000, tick)
        except tk.TclError:
            pass
    tick()

    def stop():
        _post(port, "/api/ringing/dismiss")
        _close()

    def snooze():
        _post(port, "/api/ringing/snooze")
        _close()

    def _close():
        try:
            root.quit()
            root.destroy()
        except tk.TclError:
            pass

    root.protocol("WM_DELETE_WINDOW", stop)

    btns = tk.Frame(main, bg="#2d2d2d")
    btns.pack(pady=(10, 0))
    tk.Button(btns, text="STOP", font=("Helvetica", 14, "bold"), fg="#ffffff",
              bg="#e74c3c", activebackground="#c0392b", activeforeground="#ffffff",
              width=10, height=2, command=stop).pack(side="left", padx=10)
    if snoozable:
        tk.Button(btns, text=f"SNOOZE\n({snooze_minutes} min)", font=("Helvetica", 12, "bold"),
                  fg="#ffffff", bg="#3498db", activebackground="#2980b9",
                  activeforeground="#ffffff", width=10, height=2,
                  command=snooze).pack(side="left", padx=10)

    if irritable:
        tk.Label(main, text="⚠ Irritable mode: Volume will increase!",
                 font=("Helvetica", 10), fg="#f39c12", bg="#2d2d2d").pack(pady=(15, 0))

    root.update_idletasks()
    w, h = max(root.winfo_width(), 400), max(root.winfo_height(), 300)
    x = (root.winfo_screenwidth() - w) // 2
    y = (root.winfo_screenheight() - h) // 2
    root.geometry(f"{w}x{h}+{x}+{y}")
    root.mainloop()


def main(argv=None):
    p = argparse.ArgumentParser(description="Alarm popup (daemon-spawned)")
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--label", default="Alarm")
    p.add_argument("--time", default="")
    p.add_argument("--snoozable", action="store_true")
    p.add_argument("--snooze-minutes", type=int, default=5)
    p.add_argument("--irritable", action="store_true")
    a = p.parse_args(argv)
    run_popup(a.port, a.label, a.time, a.snoozable, a.snooze_minutes, a.irritable)


if __name__ == "__main__":
    main()
