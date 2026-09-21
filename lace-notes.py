#!/usr/bin/env python3
import base64
import json
import os
import re
import shutil
import threading
import urllib.request
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gspell", "1")
from gi.repository import Gtk, Gdk, GdkPixbuf, GLib, Gspell

SAVE_DIR = os.path.expanduser("~/Documents/Lace Notes")
DRAFT_PATH = os.path.expanduser("~/.cache/lace-notes-draft.json")

# The flower icon ships alongside the script; fall back gracefully if absent.
_HERE = os.path.dirname(os.path.abspath(__file__))
FLOWER_SVG = os.path.join(_HERE, "assets", "flower.svg")

# Tag name used to attach image file paths to pixbuf anchors
IMG_PATH_ATTR = "src_path"


class LaceNotesWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="Lace Notes")
        self.set_default_size(300, 400)
        try:
            self.set_icon_from_file(FLOWER_SVG)
        except Exception:
            pass
        self.pinned = False
        self._images = []  # list of (anchor, source_path|None, display_pixbuf, original_pixbuf)

        # Enable transparency
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual:
            self.set_visual(visual)
        self.set_decorated(False)

        # Translucent styling — all child widgets transparent so the
        # window's custom-drawn background shows through uniformly
        css = Gtk.CssProvider()
        css.load_from_data(b"""
            window {
                background-color: rgba(247, 242, 230, 0.65);
            }
            window * {
                background-color: transparent;
                background-image: none;
                border: none;
                box-shadow: none;
                color: #000000;
            }
            entry {
                padding: 2px 4px;
                font-weight: bold;
            }
            textview {
                padding: 0px 2px;
            }
            button {
                padding: 4px 6px;
                min-width: 0;
                min-height: 0;
            }
            button:hover {
                background-color: rgba(0, 0, 0, 0.06);
            }
            button:active {
                background-color: rgba(0, 0, 0, 0.1);
            }
        """)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        # Edge resize support (since window is undecorated)
        self.add_events(Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.BUTTON_PRESS_MASK)
        self.connect("motion-notify-event", self._on_motion)
        self.connect("button-press-event", self._on_edge_press)
        self._resize_edge = None

        # Top-level vertical layout: drag bar + main content
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(vbox)

        # Drag bar at the top for window moving
        drag_bar = Gtk.EventBox()
        drag_bar.set_size_request(-1, 14)
        drag_bar.connect("button-press-event", self._on_drag_bar_press)
        vbox.pack_start(drag_bar, False, False, 0)

        # Main horizontal layout: content left, button column right
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        hbox.set_margin_bottom(10)
        hbox.set_margin_start(12)
        hbox.set_margin_end(4)
        vbox.pack_start(hbox, True, True, 0)

        # Left side: title + text area
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)

        # Title entry — bold, no border, placeholder only
        self.title_entry = Gtk.Entry()
        self.title_entry.set_placeholder_text("Title")
        content.pack_start(self.title_entry, False, False, 0)

        # Text area — borderless, flows below title
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.textview = Gtk.TextView()
        self.textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        scrolled.add(self.textview)
        content.pack_start(scrolled, True, True, 0)

        hbox.pack_start(content, True, True, 0)

        # Right side: vertical button column
        btn_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        btn_col.set_margin_start(4)

        # Close button (top)
        close_btn = Gtk.Button(label="\u00d7")
        close_btn.set_tooltip_text("Close")
        close_btn.connect("clicked", lambda *_: self.destroy())
        btn_col.pack_start(close_btn, False, False, 0)

        # Pin button (below close) — flower SVG icon
        self.pin_btn = Gtk.Button()
        self._flower_pixbuf = self._load_flower_svg(16)
        if self._flower_pixbuf is not None:
            self._pin_image = Gtk.Image.new_from_pixbuf(self._flower_pixbuf)
        else:
            self._pin_image = Gtk.Image.new_from_icon_name(
                "view-pin-symbolic", Gtk.IconSize.BUTTON
            )
        self._pin_image.set_opacity(0.35)
        self.pin_btn.add(self._pin_image)
        self.pin_btn.set_tooltip_text("Pin on top")
        self.pin_btn.connect("clicked", self.on_toggle_pin)
        btn_col.pack_start(self.pin_btn, False, False, 0)

        # Spacer to push save to bottom
        btn_col.pack_start(Gtk.Box(), True, True, 0)

        # Save button (bottom) — vertical 保存
        save_btn = Gtk.Button()
        save_label = Gtk.Label(label="\u4fdd\n\u5b58")
        save_btn.add(save_label)
        save_btn.set_tooltip_text("Save")
        save_btn.connect("clicked", self.on_save)
        btn_col.pack_end(save_btn, False, False, 0)

        hbox.pack_start(btn_col, False, False, 0)

        # Spell check
        gspell_view = Gspell.TextView.get_from_gtk_text_view(self.textview)
        gspell_view.basic_setup()

        # Drag-and-drop for images (files, URLs, raw image data, HTML img tags)
        targets = [
            Gtk.TargetEntry.new("text/uri-list", 0, 0),
            Gtk.TargetEntry.new("text/html", 0, 1),
            Gtk.TargetEntry.new("image/png", 0, 2),
            Gtk.TargetEntry.new("image/jpeg", 0, 3),
        ]
        self.textview.drag_dest_set(
            Gtk.DestDefaults.MOTION | Gtk.DestDefaults.HIGHLIGHT,
            targets,
            Gdk.DragAction.COPY,
        )
        self.textview.connect("drag-drop", self._on_drag_drop)
        self.textview.connect("drag-data-received", self._on_drag_data)

        # Load draft and connect change signals
        self._load_draft()
        self.title_entry.connect("changed", lambda *_: self._save_draft())
        self.textview.get_buffer().connect("changed", lambda *_: self._save_draft())
        self.connect("delete-event", self._on_close)

    def _get_edge(self, x, y):
        """Return the Gdk.WindowEdge if near a window edge, else None."""
        margin = 8
        w, h = self.get_size()
        left = x < margin
        right = x > w - margin
        top = y < margin
        bottom = y > h - margin
        if top and left:
            return Gdk.WindowEdge.NORTH_WEST
        if top and right:
            return Gdk.WindowEdge.NORTH_EAST
        if bottom and left:
            return Gdk.WindowEdge.SOUTH_WEST
        if bottom and right:
            return Gdk.WindowEdge.SOUTH_EAST
        if top:
            return Gdk.WindowEdge.NORTH
        if bottom:
            return Gdk.WindowEdge.SOUTH
        if left:
            return Gdk.WindowEdge.WEST
        if right:
            return Gdk.WindowEdge.EAST
        return None

    _EDGE_CURSORS = {
        Gdk.WindowEdge.NORTH: "n-resize",
        Gdk.WindowEdge.SOUTH: "s-resize",
        Gdk.WindowEdge.WEST: "w-resize",
        Gdk.WindowEdge.EAST: "e-resize",
        Gdk.WindowEdge.NORTH_WEST: "nw-resize",
        Gdk.WindowEdge.NORTH_EAST: "ne-resize",
        Gdk.WindowEdge.SOUTH_WEST: "sw-resize",
        Gdk.WindowEdge.SOUTH_EAST: "se-resize",
    }

    def _on_motion(self, widget, event):
        edge = self._get_edge(event.x, event.y)
        gdk_win = self.get_window()
        if edge is not None:
            cursor = Gdk.Cursor.new_from_name(self.get_display(), self._EDGE_CURSORS[edge])
            gdk_win.set_cursor(cursor)
        else:
            gdk_win.set_cursor(None)
        return False

    def _on_edge_press(self, widget, event):
        if event.button != 1:
            return False
        edge = self._get_edge(event.x, event.y)
        if edge is not None:
            self.begin_resize_drag(edge, event.button, int(event.x_root), int(event.y_root), event.time)
            return True
        # Drag-to-move — only if the click is NOT over an interactive widget
        if self._is_over_interactive(event.x, event.y):
            return False
        self.begin_move_drag(event.button, int(event.x_root), int(event.y_root), event.time)
        return True

    def _on_drag_bar_press(self, _widget, event):
        if event.button == 1:
            self.begin_move_drag(event.button, int(event.x_root), int(event.y_root), event.time)
            return True
        return False

    def _is_over_interactive(self, win_x, win_y):
        """Check if window coordinates fall over the title entry, text view, or any button."""
        for child in (self.title_entry, self.textview):
            if not child.get_realized():
                continue
            coords = child.translate_coordinates(self, 0, 0)
            if coords is None:
                continue
            cx, cy = coords
            alloc = child.get_allocation()
            if cx <= win_x < cx + alloc.width and cy <= win_y < cy + alloc.height:
                return True
        return False

    # ── Pin ──

    @staticmethod
    def _load_flower_svg(size):
        try:
            return GdkPixbuf.Pixbuf.new_from_file_at_scale(FLOWER_SVG, size, size, True)
        except GLib.Error:
            return None

    def on_toggle_pin(self, _widget):
        self.pinned = not self.pinned
        self.set_keep_above(self.pinned)
        self._pin_image.set_opacity(1.0 if self.pinned else 0.35)
        self.pin_btn.set_tooltip_text("Unpin" if self.pinned else "Pin on top")

    # ── Image drag-and-drop ──

    _IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")
    _TARGET_PRIORITY = ["image/png", "image/jpeg", "text/uri-list", "text/html"]

    def _on_drag_drop(self, widget, ctx, _x, _y, time):
        """Pick a single best target and request only that one."""
        available = [t.name() for t in ctx.list_targets()]
        for preferred in self._TARGET_PRIORITY:
            if preferred in available:
                widget.drag_get_data(ctx, Gdk.Atom.intern(preferred, False), time)
                return True
        # No image target found, let GTK handle it
        return False

    def _on_drag_data(self, _widget, ctx, _x, _y, sel, info, time):
        dtype = sel.get_data_type().name()

        # 1) Raw image data (image/png, image/jpeg)
        if dtype.startswith("image/"):
            raw = sel.get_data()
            if raw:
                self._insert_image_from_bytes(raw)
                Gtk.drag_finish(ctx, True, False, time)
                return

        # 2) text/html — browser often drops <img src="..."> HTML
        if dtype == "text/html":
            html = sel.get_data()
            if html:
                try:
                    html_str = html.decode("utf-8", errors="replace")
                except Exception:
                    html_str = str(html)
                # Strip null bytes that some browsers pad HTML with
                html_str = html_str.replace("\x00", "")
                urls = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', html_str, re.I)
                for url in urls:
                    if url.startswith("http://") or url.startswith("https://"):
                        self._download_and_insert(url)
                if urls:
                    Gtk.drag_finish(ctx, True, False, time)
                    return

        # 3) URI list — local files or http(s) URLs
        uris = sel.get_uris()
        if uris:
            for uri in uris:
                if uri.startswith("http://") or uri.startswith("https://"):
                    self._download_and_insert(uri)
                else:
                    try:
                        path = GLib.filename_from_uri(uri)[0]
                    except Exception:
                        continue
                    if not path:
                        continue
                    ext = os.path.splitext(path)[1].lower()
                    if ext in self._IMAGE_EXTS:
                        self._insert_image_from_file(path)
            Gtk.drag_finish(ctx, True, False, time)
            return

        Gtk.drag_finish(ctx, False, False, time)

    def _download_and_insert(self, url):
        """Download an image URL in a background thread, then insert on the main thread."""
        def _fetch():
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "LaceNotes/1.0"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = resp.read()
                GLib.idle_add(self._insert_image_from_bytes, data)
            except Exception:
                pass

        threading.Thread(target=_fetch, daemon=True).start()

    @staticmethod
    def _scale_for_display(pixbuf, max_w=260):
        if pixbuf.get_width() > max_w:
            ratio = max_w / pixbuf.get_width()
            return pixbuf.scale_simple(
                max_w, int(pixbuf.get_height() * ratio),
                GdkPixbuf.InterpType.BILINEAR,
            )
        return pixbuf

    def _insert_image_from_bytes(self, raw_bytes):
        """Insert an image from raw bytes (e.g. image/png drop data)."""
        try:
            loader = GdkPixbuf.PixbufLoader()
            loader.write(raw_bytes)
            loader.close()
            original = loader.get_pixbuf()
        except GLib.Error:
            return
        display = self._scale_for_display(original)
        buf = self.textview.get_buffer()
        anchor = buf.create_child_anchor(buf.get_end_iter())
        img = Gtk.Image.new_from_pixbuf(display)
        img.show()
        self.textview.add_child_at_anchor(img, anchor)
        self._images.append((anchor, None, display, original))
        buf.insert(buf.get_end_iter(), "\n")

    def _insert_image_from_file(self, path):
        try:
            original = GdkPixbuf.Pixbuf.new_from_file(path)
        except GLib.Error:
            return
        display = self._scale_for_display(original)
        buf = self.textview.get_buffer()
        anchor = buf.create_child_anchor(buf.get_end_iter())
        img = Gtk.Image.new_from_pixbuf(display)
        img.show()
        self.textview.add_child_at_anchor(img, anchor)
        self._images.append((anchor, path, display, original))
        buf.insert(buf.get_end_iter(), "\n")

    def _insert_image_from_pixbuf(self, pixbuf, src_path=None):
        """Insert an image from a pixbuf (used when restoring drafts)."""
        display = self._scale_for_display(pixbuf)
        buf = self.textview.get_buffer()
        anchor = buf.create_child_anchor(buf.get_end_iter())
        img = Gtk.Image.new_from_pixbuf(display)
        img.show()
        self.textview.add_child_at_anchor(img, anchor)
        self._images.append((anchor, src_path, display, pixbuf))

    # ── Draft auto-save ──

    def _load_draft(self):
        try:
            with open(DRAFT_PATH) as f:
                draft = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return
        self.title_entry.set_text(draft.get("title", ""))
        # Restore body text and inline images
        for segment in draft.get("segments", []):
            if segment["type"] == "text":
                buf = self.textview.get_buffer()
                buf.insert(buf.get_end_iter(), segment["data"])
            elif segment["type"] == "image":
                try:
                    raw = base64.b64decode(segment["data"])
                    loader = GdkPixbuf.PixbufLoader()
                    loader.write(raw)
                    loader.close()
                    pixbuf = loader.get_pixbuf()
                    self._insert_image_from_pixbuf(pixbuf, segment.get("src"))
                except Exception:
                    pass
        # Fallback: if no segments key, load plain body (old format)
        if "segments" not in draft and "body" in draft:
            self.textview.get_buffer().set_text(draft["body"])

    def _save_draft(self):
        buf = self.textview.get_buffer()
        segments = self._serialize_buffer(buf)
        draft = {
            "title": self.title_entry.get_text(),
            "segments": segments,
        }
        os.makedirs(os.path.dirname(DRAFT_PATH), exist_ok=True)
        with open(DRAFT_PATH, "w") as f:
            json.dump(draft, f)

    def _serialize_buffer(self, buf):
        """Serialize buffer contents to a list of text/image segments."""
        segments = []
        it = buf.get_start_iter()
        text_accum = ""

        while True:
            anchor = it.get_child_anchor()
            if anchor is not None:
                # Flush accumulated text
                if text_accum:
                    segments.append({"type": "text", "data": text_accum})
                    text_accum = ""
                # Find the image data for this anchor
                for a, src, _display, original in self._images:
                    if a is anchor:
                        success, png_data = original.save_to_bufferv("png", [], [])
                        if success:
                            segments.append({
                                "type": "image",
                                "data": base64.b64encode(png_data).decode(),
                                "src": src,
                            })
                        break
            else:
                text_accum += it.get_char()

            if not it.forward_char():
                break

        if text_accum:
            segments.append({"type": "text", "data": text_accum})
        return segments

    def _clear_draft(self):
        try:
            os.remove(DRAFT_PATH)
        except FileNotFoundError:
            pass

    def _on_close(self, _widget, _event):
        self._save_draft()
        return False

    # ── Save ──

    @staticmethod
    def _markdown_text(text):
        """Keep entered single newlines visible in Markdown renderers."""
        return re.sub(r"(?<!\n)\n(?!\n)", "  \n", text)

    def on_save(self, _widget):
        title = self.title_entry.get_text().strip()
        if not title:
            return

        os.makedirs(SAVE_DIR, exist_ok=True)
        img_dir = os.path.join(SAVE_DIR, "images")
        os.makedirs(img_dir, exist_ok=True)
        safe_title = "".join(c if c.isalnum() or c in " -_" else "_" for c in title)

        buf = self.textview.get_buffer()
        md_parts = []
        img_counter = 0
        it = buf.get_start_iter()
        text_accum = ""

        while True:
            anchor = it.get_child_anchor()
            if anchor is not None:
                if text_accum:
                    md_parts.append(self._markdown_text(text_accum))
                    text_accum = ""
                for a, src, _display, original in self._images:
                    if a is anchor:
                        img_counter += 1
                        # Determine extension from source or default to png
                        if src:
                            ext = os.path.splitext(src)[1].lower()
                            if ext not in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"):
                                ext = ".png"
                        else:
                            ext = ".png"
                        img_name = f"{safe_title}_img{img_counter}{ext}"
                        img_path = os.path.join(img_dir, img_name)
                        # Copy original file if available, otherwise save full-size pixbuf
                        if src and os.path.isfile(src):
                            shutil.copy2(src, img_path)
                        else:
                            fmt = ext.lstrip(".").replace("jpg", "jpeg")
                            original.savev(img_path, fmt, [], [])
                        md_parts.append(f"![image](images/{img_name})")
                        break
            else:
                text_accum += it.get_char()

            if not it.forward_char():
                break

        if text_accum:
            md_parts.append(self._markdown_text(text_accum))

        md_content = "".join(md_parts)
        path = os.path.join(SAVE_DIR, f"{safe_title}.md")
        with open(path, "w") as f:
            f.write(md_content)

        # Clear fields, images, and draft
        self.title_entry.set_text("")
        buf.set_text("")
        self._images.clear()
        self._clear_draft()


def main():
    win = LaceNotesWindow()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
