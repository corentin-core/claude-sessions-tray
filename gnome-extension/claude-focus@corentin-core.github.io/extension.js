import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const IFACE = `
<node>
  <interface name="io.github.corentin_core.ClaudeFocus">
    <method name="FocusWindow">
      <arg type="s" direction="in" name="pattern"/>
      <arg type="b" direction="out" name="found"/>
    </method>
    <method name="IsFocused">
      <arg type="s" direction="in" name="pattern"/>
      <arg type="b" direction="out" name="focused"/>
    </method>
    <method name="ListWindows">
      <arg type="s" direction="out" name="windows"/>
    </method>
  </interface>
</node>`;

// Title of the tray's search window (SearchWindow). Used to raise it after the
// tray creates it: the compositor can activate any window, so it bypasses
// Wayland's focus-stealing prevention.
const PALETTE_TITLE = 'search a conversation';

export default class ClaudeFocusExtension extends Extension {
    enable() {
        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE, this);
        this._dbus.export(Gio.DBus.session, '/io/github/corentin_core/ClaudeFocus');
        this._ownerId = Gio.bus_own_name(
            Gio.BusType.SESSION,
            'io.github.corentin_core.ClaudeFocus',
            Gio.BusNameOwnerFlags.NONE,
            null, null, null);

        this._settings = this.getSettings();
        Main.wm.addKeybinding(
            'toggle-palette',
            this._settings,
            Meta.KeyBindingFlags.NONE,
            Shell.ActionMode.NORMAL | Shell.ActionMode.OVERVIEW,
            () => this._openPalette());
    }

    disable() {
        Main.wm.removeKeybinding('toggle-palette');
        this._settings = null;
        if (this._raiseSource) {
            GLib.source_remove(this._raiseSource);
            this._raiseSource = 0;
        }
        if (this._dbus) {
            this._dbus.unexport();
            this._dbus = null;
        }
        if (this._ownerId) {
            Gio.bus_unown_name(this._ownerId);
            this._ownerId = 0;
        }
    }

    // Global shortcut: ask the tray to open the palette, then raise it as soon
    // as it appears. The tray and the extension are two processes; only the
    // extension (in the compositor) can grant focus under Wayland.
    _openPalette() {
        Gio.DBus.session.call(
            'io.github.corentin_core.ClaudeTray', '/io/github/corentin_core/ClaudeTray', 'io.github.corentin_core.ClaudeTray',
            'OpenSearch', null, null, Gio.DBusCallFlags.NONE, -1, null,
            (conn, res) => {
                try {
                    conn.call_finish(res);
                } catch (_e) {
                    // Tray absent: nothing to raise, give up.
                }
            });
        this._scheduleRaise();
    }

    // The window takes a moment to map: retry for ~1 s, then give up.
    _scheduleRaise() {
        if (this._raiseSource)
            GLib.source_remove(this._raiseSource);
        let tries = 0;
        this._raiseSource = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 100, () => {
            tries += 1;
            const raised = this._activateByTitle(PALETTE_TITLE);
            if (raised || tries >= 10) {
                this._raiseSource = 0;
                return GLib.SOURCE_REMOVE;
            }
            return GLib.SOURCE_CONTINUE;
        });
    }

    _activateByTitle(needle) {
        needle = (needle || '').toLowerCase();
        if (!needle)
            return false;
        for (const actor of global.get_window_actors()) {
            const win = actor.meta_window;
            if ((win.get_title() || '').toLowerCase().includes(needle)) {
                win.activate(global.get_current_time());
                return true;
            }
        }
        return false;
    }

    // Activate the first VSCode window whose title contains `pattern`. A window
    // is recognized as VSCode by its wm_class OR the "visual studio code" title
    // signature -> never a Firefox tab with a close-looking title.
    FocusWindow(pattern) {
        const needle = (pattern || '').toLowerCase();
        if (!needle)
            return false;
        for (const actor of global.get_window_actors()) {
            const win = actor.meta_window;
            const wmClass = (win.get_wm_class() || '').toLowerCase();
            const title = (win.get_title() || '').toLowerCase();
            const isVSCode = wmClass.includes('code') || title.includes('visual studio code');
            if (isVSCode && title.includes(needle)) {
                win.activate(global.get_current_time());
                return true;
            }
        }
        return false;
    }

    // True if the active window is a VSCode window whose title contains
    // `pattern`: same recognition as FocusWindow, but on the focused window
    // only. Lets the tray skip notifying a session already in view.
    IsFocused(pattern) {
        const needle = (pattern || '').toLowerCase();
        if (!needle)
            return false;
        const win = global.display.get_focus_window();
        if (!win)
            return false;
        const wmClass = (win.get_wm_class() || '').toLowerCase();
        const title = (win.get_title() || '').toLowerCase();
        const isVSCode = wmClass.includes('code') || title.includes('visual studio code');
        return isVSCode && title.includes(needle);
    }

    // Diagnostic: "wm_class :: title" per window (Eval being blocked).
    ListWindows() {
        return global.get_window_actors()
            .map(a => `${a.meta_window.get_wm_class()} :: ${a.meta_window.get_title()}`)
            .join('\n');
    }
}
