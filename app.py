#!/usr/bin/env python3
"""
app.py
======

Desktop UI for supplier_pipeline.py.

- Settings screen: Cerve client_id / client_secret + Gemini API key. Saved as
  JSON in the OS app-data directory.
- Main screen: pick xlsx/csv → enter supplier UUID → Run → live log.
- Settings opens automatically the first time (when no creds saved).

Run:
    uv run app.py
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import uuid
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
import keyring

from _version import __version__

APP_NAME = "SupplierPipeline"
KEYRING_SERVICE = APP_NAME


# ---------------------------------------------------------------------------
# Config (JSON in OS app-data dir)
# ---------------------------------------------------------------------------
def config_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / APP_NAME


LEGACY_CONFIG_PATH = config_dir() / "config.json"  # pre-0.4 plaintext creds
SKU_COLUMNS_PATH = config_dir() / "sku_columns.json"
CREDENTIAL_KEYS = ("cerve_client_id", "cerve_client_secret", "gemini_api_key")
# All credentials live in a single keychain item (one JSON blob) so the OS
# only ever prompts once for access, not once per credential.
CREDENTIAL_BLOB_KEY = "credentials"


_creds_cache: dict | None = None


def _migrate_to_blob() -> dict | None:
    """One-time: fold the old per-credential keychain items into the single
    blob item, then delete them. Returns the migrated creds, or None if there
    was nothing to migrate."""
    out: dict[str, str] = {}
    for k in CREDENTIAL_KEYS:
        try:
            v = keyring.get_password(KEYRING_SERVICE, k)
        except keyring.errors.KeyringError:
            v = None
        if v:
            out[k] = v
    if not out:
        return None
    try:
        keyring.set_password(KEYRING_SERVICE, CREDENTIAL_BLOB_KEY, json.dumps(out))
    except keyring.errors.KeyringError:
        return out  # couldn't write blob; still return what we read
    for k in CREDENTIAL_KEYS:
        try:
            keyring.delete_password(KEYRING_SERVICE, k)
        except keyring.errors.KeyringError:
            pass
    return out


def load_credentials(force: bool = False) -> dict:
    """Read the credentials from the OS keychain.

    All three credentials are stored as a single JSON blob under one keychain
    item, so a fresh read triggers at most one OS password prompt. The result
    is cached for the lifetime of the process so repeated calls don't re-hit
    the keychain. Pass force=True to bypass the cache and re-read."""
    global _creds_cache
    if _creds_cache is not None and not force:
        return _creds_cache
    try:
        raw = keyring.get_password(KEYRING_SERVICE, CREDENTIAL_BLOB_KEY)
    except keyring.errors.KeyringError:
        raw = None
    if raw:
        try:
            out = {k: v for k, v in json.loads(raw).items() if v}
        except (json.JSONDecodeError, AttributeError):
            out = {}
    else:
        # No blob yet — fold any old per-credential items into one.
        out = _migrate_to_blob() or {}
    _creds_cache = out
    return out


def save_credentials(creds: dict) -> None:
    """Write the credentials to the OS keychain as a single JSON blob."""
    global _creds_cache
    out = {k: v for k in CREDENTIAL_KEYS if (v := (creds.get(k) or "").strip())}
    if out:
        keyring.set_password(KEYRING_SERVICE, CREDENTIAL_BLOB_KEY, json.dumps(out))
    # Invalidate the cache so the next load reflects what we just wrote.
    _creds_cache = None


def have_all_creds(cfg: dict) -> bool:
    return all(cfg.get(k) for k in CREDENTIAL_KEYS)


def migrate_legacy_config() -> None:
    """One-time: move credentials from the old plaintext config.json into the
    OS keychain, then delete the file. Safe to call on every launch."""
    if not LEGACY_CONFIG_PATH.is_file():
        return
    try:
        data = json.loads(LEGACY_CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(data, dict):
        return
    creds = {k: data[k] for k in CREDENTIAL_KEYS if data.get(k)}
    if creds:
        try:
            keyring.set_password(
                KEYRING_SERVICE, CREDENTIAL_BLOB_KEY, json.dumps(creds)
            )
        except keyring.errors.KeyringError:
            return
        try:
            LEGACY_CONFIG_PATH.unlink()
        except OSError:
            pass


def load_user_sku_columns() -> list[str]:
    """User-discovered SKU column names, persisted across versions."""
    if not SKU_COLUMNS_PATH.is_file():
        return []
    try:
        data = json.loads(SKU_COLUMNS_PATH.read_text(encoding="utf-8"))
        return [str(x) for x in data] if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def remember_sku_column(name: str) -> None:
    """Append `name` to the user candidates list if it's new."""
    name = (name or "").strip()
    if not name:
        return
    existing = load_user_sku_columns()
    if name in existing:
        return
    existing.append(name)
    SKU_COLUMNS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SKU_COLUMNS_PATH.write_text(json.dumps(existing, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Pending-jobs queue (UI adds, worker drains)
# ---------------------------------------------------------------------------
class QueueManager:
    """Thread-safe FIFO of pending pipeline jobs. UI can add/remove at any time;
    the worker calls take() with a timeout and exits when the queue stays empty.

    Each job is a dict with at least: id, file_path, supplier_id, sku_column.
    The `id` is a uuid string set on add(), used to target removals safely
    (positions shift as the worker pops the front)."""

    def __init__(self):
        self._items: list[dict] = []
        self._cond = threading.Condition()

    def add(self, item: dict) -> None:
        with self._cond:
            self._items.append(item)
            self._cond.notify()

    def remove_by_id(self, item_id: str) -> None:
        with self._cond:
            self._items = [i for i in self._items if i.get("id") != item_id]

    def snapshot(self) -> list[dict]:
        with self._cond:
            return list(self._items)

    def take(self, timeout: float) -> dict | None:
        """Block up to `timeout` seconds waiting for an item. Returns the next
        item, or None if the wait timed out with the queue still empty."""
        with self._cond:
            if not self._items:
                self._cond.wait(timeout=timeout)
            return self._items.pop(0) if self._items else None


# ---------------------------------------------------------------------------
# Pipeline worker (runs in a background thread)
# ---------------------------------------------------------------------------
class _QueueWriter:
    """File-like sink that pushes complete lines to a queue."""
    def __init__(self, q: queue.Queue):
        self.q = q
        self.buf = ""

    def write(self, s: str) -> int:
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.q.put(line + "\n")
        return len(s)

    def flush(self) -> None:
        if self.buf:
            self.q.put(self.buf)
            self.buf = ""


def run_pipeline_sync(
    file_path: str,
    supplier_id: str,
    sku_column: str,
    cfg: dict,
    log_q: queue.Queue,
) -> tuple[bool, str | None]:
    """Run supplier_pipeline.main() once, synchronously. Returns (ok, err)."""
    ok = False
    err: str | None = None
    try:
        os.environ["GEMINI_API_KEY"] = cfg["gemini_api_key"]
        os.environ["CERVE_CLIENT_ID"] = cfg["cerve_client_id"]
        os.environ["CERVE_CLIENT_SECRET"] = cfg["cerve_client_secret"]

        old_stdout, old_stderr, old_argv = sys.stdout, sys.stderr, sys.argv
        sys.stdout = _QueueWriter(log_q)
        sys.stderr = _QueueWriter(log_q)
        argv = ["supplier_pipeline", file_path, "--supplier-id", supplier_id]
        if sku_column:
            argv += ["--sku-column", sku_column]
        sys.argv = argv
        try:
            import supplier_pipeline  # imported lazily so a bad import doesn't break the UI
            try:
                supplier_pipeline.main()
                ok = True
            except SystemExit as e:
                code = e.code if e.code is not None else 0
                ok = code == 0
                if not ok:
                    err = str(e.code) if isinstance(e.code, str) else f"exit code {code}"
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            sys.stdout, sys.stderr, sys.argv = old_stdout, old_stderr, old_argv
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
    return ok, err


# ---------------------------------------------------------------------------
# UI: Settings frame
# ---------------------------------------------------------------------------
class SettingsFrame(ctk.CTkFrame):
    FIELDS = [
        ("cerve_client_id", "Cerve Client ID", None),
        ("cerve_client_secret", "Cerve Client Secret", "*"),
        ("gemini_api_key", "Gemini API Key", "*"),
    ]

    def __init__(self, master, on_save, can_go_back: bool):
        super().__init__(master)

        # Header with optional back button
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(15, 10))
        ctk.CTkLabel(top, text="Settings", font=ctk.CTkFont(size=22, weight="bold")).pack(side="left")
        if can_go_back:
            ctk.CTkButton(top, text="← Back", width=100, command=lambda: on_save(None)).pack(side="right")

        self.fields: dict[str, ctk.CTkEntry] = {}
        cfg = load_credentials()
        for key, label, show in self.FIELDS:
            ctk.CTkLabel(self, text=label, anchor="w").pack(fill="x", padx=30, pady=(12, 2))
            e = ctk.CTkEntry(self, show=show)
            e.pack(fill="x", padx=30)
            if cfg.get(key):
                e.insert(0, cfg[key])
            self.fields[key] = e

        ctk.CTkLabel(
            self,
            text="Stored securely in your system keychain",
            text_color="gray",
            font=ctk.CTkFont(size=10),
        ).pack(pady=(20, 5))

        ctk.CTkButton(self, text="Save", height=36, command=lambda: self._save(on_save)).pack(pady=10)

    def _save(self, on_save):
        new = {k: e.get().strip() for k, e in self.fields.items()}
        if not all(new.values()):
            messagebox.showerror("Missing values", "Please fill all three fields.")
            return
        try:
            save_credentials(new)
        except keyring.errors.KeyringError as e:
            messagebox.showerror("Keychain error", f"Couldn't save to system keychain:\n{e}")
            return
        on_save(new)


# ---------------------------------------------------------------------------
# UI: Main frame
# ---------------------------------------------------------------------------
WORKER_IDLE_TIMEOUT = 5.0  # seconds the worker waits for a new job before exiting


class MainFrame(ctk.CTkFrame):
    def __init__(self, master, on_open_settings):
        super().__init__(master)
        self.file_path: str | None = None
        self.log_q: queue.Queue = queue.Queue()
        self.queue_mgr = QueueManager()
        self.worker_thread: threading.Thread | None = None

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(15, 5))
        ctk.CTkLabel(top, text="Supplier Pipeline", font=ctk.CTkFont(size=22, weight="bold")).pack(side="left")
        ctk.CTkButton(top, text="⚙ Settings", width=120, command=on_open_settings).pack(side="right")

        fp = ctk.CTkFrame(self, fg_color="transparent")
        fp.pack(fill="x", padx=20, pady=10)
        ctk.CTkButton(fp, text="Choose file…", width=130, command=self._pick_file).pack(side="left")
        self.file_label = ctk.CTkLabel(fp, text="no file selected", anchor="w", text_color="gray")
        self.file_label.pack(side="left", padx=10, fill="x", expand=True)

        ctk.CTkLabel(self, text="SKU column", anchor="w").pack(fill="x", padx=20, pady=(10, 2))
        self.sku_col_combo = ctk.CTkComboBox(self, values=["(pick a file first)"], state="disabled")
        self.sku_col_combo.pack(fill="x", padx=20)
        self.sku_col_hint = ctk.CTkLabel(self, text="", anchor="w", text_color="gray",
                                         font=ctk.CTkFont(size=10))
        self.sku_col_hint.pack(fill="x", padx=20)

        ctk.CTkLabel(self, text="Supplier ID (Cerve UUID)", anchor="w").pack(fill="x", padx=20, pady=(10, 2))
        self.supplier_entry = ctk.CTkEntry(self, placeholder_text="e.g. 7483595f-fd09-4977-b59c-3ab0180b59ee")
        self.supplier_entry.pack(fill="x", padx=20)

        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(pady=15)
        ctk.CTkButton(btn_row, text="+ Add to queue", height=36, width=160,
                      command=self._add_to_queue).pack(side="left", padx=5)
        self.run_btn = ctk.CTkButton(btn_row, text="Run all", height=36, width=160,
                                     font=ctk.CTkFont(size=14, weight="bold"),
                                     command=self._run_all,
                                     state="disabled")  # empty queue at startup
        self.run_btn.pack(side="left", padx=5)

        # Queue panel (above the log)
        self.queue_header = ctk.CTkLabel(self, text="Queue (empty)", anchor="w",
                                         font=ctk.CTkFont(size=12, weight="bold"))
        self.queue_header.pack(fill="x", padx=20, pady=(5, 2))
        self.queue_list = ctk.CTkScrollableFrame(self, height=120)
        self.queue_list.pack(fill="x", padx=20, pady=(0, 10))

        self.log = ctk.CTkTextbox(self, height=200, font=("Menlo", 11))
        self.log.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        self.log.configure(state="disabled")

        self.after(150, self._drain_log)

    # --- file pick / SKU column auto-detect ------------------------------
    def _pick_file(self):
        p = filedialog.askopenfilename(
            title="Choose supplier file",
            filetypes=[("Spreadsheets", "*.csv *.xlsx *.xls"), ("All files", "*.*")],
        )
        if not p:
            return
        self.file_path = p
        self.file_label.configure(text=Path(p).name, text_color=("black", "white"))
        self._populate_sku_combo(Path(p))

    def _populate_sku_combo(self, src: Path):
        try:
            import supplier_pipeline
            headers = supplier_pipeline.peek_headers(src)
        except Exception as e:  # noqa: BLE001
            self.sku_col_combo.configure(values=["(error reading file)"], state="disabled")
            self.sku_col_combo.set("(error reading file)")
            self.sku_col_hint.configure(text=f"{type(e).__name__}: {e}", text_color="red")
            return
        if not headers:
            self.sku_col_combo.configure(values=["(no headers found)"], state="disabled")
            self.sku_col_combo.set("(no headers found)")
            return
        self.sku_col_combo.configure(values=headers, state="normal")
        guess = supplier_pipeline.auto_detect_sku_column(headers, load_user_sku_columns())
        if guess:
            self.sku_col_combo.set(guess)
            self.sku_col_hint.configure(text=f"auto-detected: {guess}", text_color="gray")
        else:
            self.sku_col_combo.set(headers[0])
            self.sku_col_hint.configure(
                text="no known SKU column matched — please pick the right one",
                text_color="orange",
            )

    # --- log textbox -----------------------------------------------------
    def _append(self, text: str):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _drain_log(self):
        try:
            while True:
                self._append(self.log_q.get_nowait())
        except queue.Empty:
            pass
        self.after(150, self._drain_log)

    # --- queue management ------------------------------------------------
    def _add_to_queue(self):
        if not self.file_path:
            messagebox.showerror("Missing file", "Pick a supplier file first.")
            return
        supplier_id = self.supplier_entry.get().strip()
        if not supplier_id:
            messagebox.showerror("Missing supplier ID", "Enter the Cerve supplier UUID.")
            return
        sku_column = (self.sku_col_combo.get() or "").strip()
        if not sku_column or sku_column.startswith("("):
            messagebox.showerror("Missing SKU column", "Pick the column that holds the supplier's SKU code.")
            return
        if not have_all_creds(load_credentials()):
            messagebox.showerror("Missing credentials", "Open Settings and fill in credentials first.")
            return

        remember_sku_column(sku_column)
        item = {
            "id": uuid.uuid4().hex,
            "file_path": self.file_path,
            "supplier_id": supplier_id,
            "sku_column": sku_column,
        }
        self.queue_mgr.add(item)
        self._refresh_queue_view()
        # Clear the file + sku column (keep supplier_id for repeat adds against
        # the same supplier — common case)
        self.file_path = None
        self.file_label.configure(text="no file selected", text_color="gray")
        self.sku_col_combo.configure(values=["(pick a file first)"], state="disabled")
        self.sku_col_combo.set("(pick a file first)")
        self.sku_col_hint.configure(text="")

    def _sync_run_button_state(self):
        """Run all is enabled only when there's work AND no worker running."""
        worker_alive = self.worker_thread is not None and self.worker_thread.is_alive()
        has_items = bool(self.queue_mgr.snapshot())
        if worker_alive:
            self.run_btn.configure(state="disabled", text="Running…")
        elif has_items:
            self.run_btn.configure(state="normal", text="Run all")
        else:
            self.run_btn.configure(state="disabled", text="Run all")

    def _refresh_queue_view(self):
        # Wipe + repopulate the scrollable list. Cheap enough for our scale.
        for w in self.queue_list.winfo_children():
            w.destroy()
        items = self.queue_mgr.snapshot()
        if items:
            self.queue_header.configure(text=f"Queue ({len(items)} pending)")
        else:
            self.queue_header.configure(text="Queue (empty)")
        self._sync_run_button_state()
        for item in items:
            row = ctk.CTkFrame(self.queue_list, fg_color="transparent")
            row.pack(fill="x", pady=2)
            label = (
                f"{Path(item['file_path']).name}   ·   "
                f"supplier {item['supplier_id'][:8]}…   ·   "
                f"col '{item['sku_column']}'"
            )
            ctk.CTkLabel(row, text=label, anchor="w").pack(side="left", fill="x", expand=True)
            iid = item["id"]
            ctk.CTkButton(
                row, text="✕", width=28, height=24, fg_color="transparent",
                hover_color=("gray80", "gray30"),
                command=lambda x=iid: self._remove_from_queue(x),
            ).pack(side="right")

    def _remove_from_queue(self, item_id: str):
        self.queue_mgr.remove_by_id(item_id)
        self._refresh_queue_view()

    # --- worker ---------------------------------------------------------
    def _run_all(self):
        if self.worker_thread and self.worker_thread.is_alive():
            return  # already running
        if not have_all_creds(load_credentials()):
            messagebox.showerror("Missing credentials", "Open Settings and fill in credentials first.")
            return
        self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker_thread.start()
        self._sync_run_button_state()

    def _worker_loop(self):
        """Pull jobs from the queue and run them. Exits if the queue stays
        empty for WORKER_IDLE_TIMEOUT seconds — clicking Run all restarts."""
        try:
            while True:
                item = self.queue_mgr.take(timeout=WORKER_IDLE_TIMEOUT)
                if item is None:
                    break  # idle timeout
                # Refresh UI now that this item has been popped off the front
                self.after(0, self._refresh_queue_view)
                self.log_q.put(
                    f"\n=== {Path(item['file_path']).name} → supplier "
                    f"{item['supplier_id']} (col '{item['sku_column']}') ===\n"
                )
                cfg = load_credentials()
                ok, err = run_pipeline_sync(
                    item["file_path"], item["supplier_id"],
                    item["sku_column"], cfg, self.log_q,
                )
                self._drain_log_now_via_main_thread()
                msg = "OK" if ok else "with errors"
                self.log_q.put(f"--- {Path(item['file_path']).name} finished {msg} ---\n")
                if err:
                    self.log_q.put(f"error: {err}\n")
        finally:
            self.after(0, self._on_worker_exit)

    def _drain_log_now_via_main_thread(self):
        """Ensure the textbox shows everything the just-finished job emitted
        before the next item's banner. Schedule a drain on the main thread."""
        evt = threading.Event()

        def drain():
            try:
                while True:
                    self._append(self.log_q.get_nowait())
            except queue.Empty:
                pass
            evt.set()

        self.after(0, drain)
        evt.wait(timeout=2)

    def _on_worker_exit(self):
        # Final drain + UI restore
        try:
            while True:
                self._append(self.log_q.get_nowait())
        except queue.Empty:
            pass
        self._sync_run_button_state()
        if self.queue_mgr.snapshot():
            # Queue isn't empty — the worker hit the idle timeout right before
            # something landed. Surface that so the user knows to click Run.
            self._append("\n[queue] worker exited; click Run all to resume.\n")
        else:
            self._append("\n[queue] all done.\n")


# ---------------------------------------------------------------------------
# Root app
# ---------------------------------------------------------------------------
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.title(f"{APP_NAME} {__version__}")
        self.geometry("680x640")
        self.minsize(560, 560)
        self._current: ctk.CTkFrame | None = None

        migrate_legacy_config()
        if have_all_creds(load_credentials()):
            self.show_main()
        else:
            self.show_settings(can_go_back=False)

    def _swap(self, frame_cls, **kwargs):
        if self._current is not None:
            self._current.destroy()
        self._current = frame_cls(self, **kwargs)
        self._current.pack(fill="both", expand=True)

    def show_main(self):
        self._swap(MainFrame, on_open_settings=lambda: self.show_settings(can_go_back=True))

    def show_settings(self, can_go_back: bool):
        self._swap(SettingsFrame, on_save=lambda _cfg: self.show_main(), can_go_back=can_go_back)


if __name__ == "__main__":
    App().mainloop()
