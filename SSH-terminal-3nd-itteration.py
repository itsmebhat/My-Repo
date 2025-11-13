# -*- coding: utf-8 -*-
"""
MS-Term Pro — Multi-tab PuTTY-like SSH/Telnet Client (Tkinter)

BUILD_ID: 2025-09-10-b  (Includes requested changes)

Included changes (explicit):
  [CHANGE-1] "Use default credentials" checkbox with logic:
      • When checked, uses username/password from JSON file if available.
      • When unchecked, manual Username/Password fields are enabled.
      • If checked but defaults are missing, warns and reverts to manual mode.
  [CHANGE-2] Controls moved to TOP in two rows:
      • Row 0: Protocol, Host, Port, Connect, Disconnect
      • Row 1: Use default credentials (checkbox), User, Password, Jump (bastion)
      • Terminal below these controls.
  [CHANGE-3] Label is "Host" instead of "IP address"; IP/host history preserved.
  [CHANGE-4] Mac-like monospace auto-pick (SF Mono,… Consolas) with default size 12.

Dependencies: paramiko, telnetlib3
"""
import tkinter as tk
from tkinter import messagebox, ttk, filedialog
from tkinter import scrolledtext
from tkinter import font as tkfont
from tkinter import colorchooser
import asyncio
import threading
import json
import os
import sys
import time
import difflib
import re
from pathlib import Path

# ----------------------- Storage & Config -----------------------
DEFAULT_BASE = Path.home() / 'Documents' / 'MSTerminal'
BASE_DIR = Path(os.environ.get('MSTERM_BASE', str(DEFAULT_BASE))).expanduser()
(BASE_DIR / 'logs').mkdir(parents=True, exist_ok=True)
(BASE_DIR / 'sessions').mkdir(parents=True, exist_ok=True)
(BASE_DIR / 'profiles').mkdir(parents=True, exist_ok=True)
CONFIG_FILE = BASE_DIR / 'config.json'
SNIPPETS_FILE = BASE_DIR / 'snippets.json'
IP_HISTORY_FILE = BASE_DIR / 'recent_ips.json'
KNOWN_HOSTS_FILE = BASE_DIR / 'known_hosts'

DEFAULT_CONFIG = {
    "theme": "light",  # light|dark
    "font_family": "Menlo",
    "font_size": 12,  # default bumped to 12
    "scrollback_limit": 8000,
    "timestamps": False,
    "auto_log_default": True,
    "log_max_bytes": 5 * 1024 * 1024,
    "ansi_colors": True,
    "keepalive_seconds": 30,
    "ssh_port": 22,
    "telnet_port": 23,
    "idle_warn_seconds": 600,
    "paste_throttle_ms": 8,
    "enter_mode": "CR",     # "CR" or "CRLF"
    "backspace_mode": "DEL" # "DEL" (0x7F) or "BS" (0x08)

}
CONFIG = DEFAULT_CONFIG.copy()

def load_json(path, default):
    try:
        p = Path(path)
        if p.exists():
            with open(p, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return default

def save_json(path, data):
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

# Load config/snippets/history
CONFIG.update(load_json(CONFIG_FILE, {}))
SNIPPETS = load_json(SNIPPETS_FILE, [
    {"name": "Show version", "text": "show version"},
    {"name": "Save config", "text": "write memory"},
])
IP_HISTORY = load_json(IP_HISTORY_FILE, [])
if not isinstance(IP_HISTORY, list):
    IP_HISTORY = []

def save_ip_to_history(host):
    if host and host not in IP_HISTORY:
        IP_HISTORY.append(host)
        save_json(IP_HISTORY_FILE, IP_HISTORY)

# ----------------------- Credentials -----------------------
# Primary path (example per your environment) and fallback inside BASE_DIR
CRED_PRIMARY = Path(r'C:/Users/pravek20/credentials_file.json')
CRED_FALLBACK = BASE_DIR / 'credentials.json'
USERNAME = PASSWORD = None
for cred_path in (CRED_PRIMARY, CRED_FALLBACK):
    try:
        if Path(cred_path).exists():
            c = load_json(cred_path, {})
            USERNAME, PASSWORD = c.get('username'), c.get('password')
            if USERNAME and PASSWORD:
                break
    except Exception:
        pass

# ----------------------- Init (optional) -----------------------
INIT_COMMANDS = []  # e.g., ['terminal length 0']
SEND_INIT_CMDS = True

# ----------------------- ANSI Support -----------------------
ANSI_COLORS = {
    30: ('#000000', None), 31: ('#cc0000', None), 32: ('#00aa00', None), 33: ('#999900', None),
    34: ('#0033cc', None), 35: ('#990099', None), 36: ('#008888', None), 37: ('#cccccc', None),
    90: ('#666666', None), 91: ('#ff4444', None), 92: ('#22cc22', None), 93: ('#dddd22', None),
    94: ('#3366ff', None), 95: ('#cc33cc', None), 96: ('#22cccc', None), 97: ('#ffffff', None),
}
SGR_RE = re.compile(r"\[([0-9;]*)m")

# ----------------------- Session Tab -----------------------
class SessionTab:
    def __init__(self, app, name: str):
        self.app = app
        self.name = name
        self.frame = tk.Frame(app.notebook)

        # State
        self.protocol_var = tk.StringVar(value='ssh')
        self.ip_var = tk.StringVar()
        self.port_var = tk.StringVar(value=str(CONFIG.get('ssh_port', 22)))
        self.jump_var = tk.StringVar()
        self.auto_reconnect = tk.BooleanVar(value=True)
        self.auto_log = tk.BooleanVar(value=CONFIG.get('auto_log_default', True))
        self.prompt_mode = tk.StringVar(value='?')
        self.connected = False
        self.last_rx_time = time.time()

        # Connections
        self.ssh_client = None
        self.ssh_channel = None
        self.telnet_reader = None
        self.telnet_writer = None
        self.telnet_loop = None
        self.telnet_thread = None
        self.reader_thread = None

        # Logging
        self.log_fh = None
        self.current_log_bytes = 0

        # --- [CHANGE-4] Font (Mac-like auto-pick), default size 12 ---
        def _pick_mac_like_font():
            try:
                families = set(tkfont.families())
            except Exception:
                families = set()
            for f in ("SF Mono", "SFMono-Regular", "Menlo", "Monaco", "Hack", "DejaVu Sans Mono", "Cascadia Mono", "Consolas"):
                if f in families:
                    return f
            return CONFIG.get('font_family', 'Consolas')
        self.term_font = tkfont.Font(family=_pick_mac_like_font(), size=max(12, CONFIG.get('font_size', 12)))

        # --- [CHANGE-1] Credentials UI state ---
        self.use_default_creds = tk.BooleanVar(value=bool(USERNAME and PASSWORD))
        self.user_var = tk.StringVar(value=USERNAME or "")
        self.pass_var = tk.StringVar(value=PASSWORD or "")

        # =================== [CHANGE-2] Header (Top) ===================
        hdr = tk.Frame(self.frame)
        hdr.grid(row=0, column=0, sticky='ew', padx=8, pady=(8, 4))
        for c in range(10):
            hdr.columnconfigure(c, weight=0)
        hdr.columnconfigure(3, weight=2)  # Host expands
        hdr.columnconfigure(7, weight=1)  # Jump expands a bit

        # Row 0: Protocol, Host, Port, Connect / Disconnect
        ttk.Label(hdr, text='Protocol').grid(row=0, column=0, sticky='w')
        self.protocol_dropdown = ttk.Combobox(hdr, textvariable=self.protocol_var, values=['ssh', 'telnet'], width=8)
        self.protocol_dropdown.grid(row=0, column=1, sticky='w', padx=(4, 12))

        ttk.Label(hdr, text='Host').grid(row=0, column=2, sticky='e')  # [CHANGE-3]
        self.ip_dropdown = ttk.Combobox(hdr, textvariable=self.ip_var, values=self.app.get_ip_history(), width=32)
        self.ip_dropdown.grid(row=0, column=3, sticky='ew', padx=(4, 12))

        ttk.Label(hdr, text='Port').grid(row=0, column=4, sticky='e')
        self.port_entry = ttk.Entry(hdr, textvariable=self.port_var, width=7)
        self.port_entry.grid(row=0, column=5, sticky='w', padx=(4, 12))

        self.connect_btn = tk.Button(hdr, text='Connect', width=10, command=self.connect_device)
        self.connect_btn.grid(row=0, column=8, sticky='e', padx=(4, 4))
        self.disconnect_btn = tk.Button(hdr, text='Disconnect', width=10, command=self.disconnect)
        self.disconnect_btn.grid(row=0, column=9, sticky='e')

        # Row 1: Use default credentials (checkbox), User, Password, Jump
        self.use_default_chk = ttk.Checkbutton(
            hdr, text='Use default credentials', variable=self.use_default_creds, command=self.on_toggle_default_creds
        )
        self.use_default_chk.grid(row=1, column=0, columnspan=2, sticky='w', pady=(6, 0))

        ttk.Label(hdr, text='User').grid(row=1, column=2, sticky='e', pady=(6, 0))
        self.user_entry = ttk.Entry(hdr, textvariable=self.user_var, width=18)
        self.user_entry.grid(row=1, column=3, sticky='w', padx=(4, 12), pady=(6, 0))

        ttk.Label(hdr, text='Password').grid(row=1, column=4, sticky='e', pady=(6, 0))
        self.pass_entry = ttk.Entry(hdr, textvariable=self.pass_var, width=18, show='•')
        self.pass_entry.grid(row=1, column=5, sticky='w', padx=(4, 12), pady=(6, 0))

        ttk.Label(hdr, text='Jump').grid(row=1, column=6, sticky='e', pady=(6, 0))
        self.jump_entry = ttk.Entry(hdr, textvariable=self.jump_var, width=20)
        self.jump_entry.grid(row=1, column=7, sticky='ew', padx=(4, 12), pady=(6, 0))




        # =================== Terminal ===================
        self.terminal = scrolledtext.ScrolledText(self.frame, wrap='none', font=self.term_font, undo=False, autoseparators=False)
        self.terminal.grid(row=2, column=0, sticky='nsew', padx=8, pady=(4, 4))
        self.apply_theme()
        self.install_ansi_tags()

        # =================== Status Row ===================
        self.status_var = tk.StringVar(value='Disconnected')
        status_bar = tk.Frame(self.frame)
        status_bar.grid(row=3, column=0, sticky='ew', padx=8, pady=(0, 8))
        tk.Label(status_bar, textvariable=self.status_var, anchor='w').pack(side='left')
        tk.Label(status_bar, textvariable=self.prompt_mode, width=6, anchor='w').pack(side='left', padx=(8, 0))
        ttk.Checkbutton(status_bar, text='Auto-reconnect', variable=self.auto_reconnect).pack(side='left', padx=(12, 0))
        ttk.Checkbutton(status_bar, text='Auto-log', variable=self.auto_log).pack(side='left', padx=(12, 0))

        # Layout weights
        self.frame.rowconfigure(0, weight=0)
        self.frame.rowconfigure(1, weight=0)
        self.frame.rowconfigure(2, weight=1)
        self.frame.rowconfigure(3, weight=0)
        self.frame.columnconfigure(0, weight=1)

        # Context menu
        self.menu = tk.Menu(self.frame, tearoff=0)
        self.menu.add_command(label='Copy', command=lambda: self.terminal.event_generate('<<Copy>>'))
        self.menu.add_command(label='Paste', command=lambda: self.on_paste())
        self.menu.add_separator()
        self.menu.add_command(label='Clear Screen', command=self.clear_screen)
        self.menu.add_command(label='Save Buffer (TXT)', command=self.save_buffer_txt)
        self.menu.add_command(label='Export Buffer (HTML)', command=self.save_buffer_html)
        self.menu.add_separator()
        self.menu.add_command(label='Rename Tab…', command=self.rename_tab)
        self.terminal.bind('<Button-3>', lambda e: (self.menu.tk_popup(e.x_root, e.y_root), 'break'))

        # Key bindings
        self.terminal.bind('<Key>', self.on_keypress)
        self.terminal.bind('<<Paste>>', self.on_paste)
        self.terminal.bind('<Control-v>', self.on_paste)
        self.terminal.bind('<Configure>', lambda e: self.schedule_resize_pty())

        # Initialize credential field states
        self.on_toggle_default_creds()


    def open_font_dialog(self):
        top = tk.Toplevel(self.frame)
        top.title("Font Settings")
        families = sorted(tkfont.families())
        font_var = tk.StringVar(value=self.term_font.actual('family'))
        size_var = tk.IntVar(value=self.term_font.actual('size'))
        ttk.Label(top, text="Font Family:").pack(anchor='w', padx=8, pady=4)
        font_dropdown = ttk.Combobox(top, textvariable=font_var, values=families, width=24)
        font_dropdown.pack(anchor='w', padx=8, pady=4)
        ttk.Label(top, text="Font Size:").pack(anchor='w', padx=8, pady=4)
        size_entry = ttk.Entry(top, textvariable=size_var, width=6)
        size_entry.pack(anchor='w', padx=8, pady=4)
        def apply_font():
            self.term_font.configure(family=font_var.get(), size=size_var.get())
            self.terminal.configure(font=self.term_font)
            top.destroy()
        ttk.Button(top, text="Apply", command=apply_font).pack(anchor='e', padx=8, pady=8)

    def open_color_dialog(self):
        fg = colorchooser.askcolor(title="Choose Text Color")[1]
        bg = colorchooser.askcolor(title="Choose Background Color")[1]
        if fg:
            self.terminal.configure(fg=fg, insertbackground=fg)
        if bg:
            self.terminal.configure(bg=bg)



        self.safe_log(f'[{self.name}] Ready. Storage: {BASE_DIR}')

    # ---- Theme & ANSI ----
    def apply_theme(self):
        theme = CONFIG.get('theme', 'light')
        if theme == 'dark':
            bg, fg = '#1e1e1e', '#eaeaea'
        else:
            bg, fg = '#ffffff', '#000000'
        try:
            self.terminal.configure(bg=bg, fg=fg, insertbackground=fg)
        except Exception:
            pass

    def install_ansi_tags(self):
        for code, (fg, _bg) in ANSI_COLORS.items():
            try:
                self.terminal.tag_configure(f'sgr_{code}', foreground=fg)
            except Exception:
                pass

    # ---- Status/logging ----
    def update_status(self, text):
        self.app.root.after(0, lambda: self.status_var.set(text))

    def update_prompt_mode(self, text):
        mode = 'user'
        if '#' in text:
            mode = 'priv'
        elif '>' in text:
            mode = 'user'
        elif '$' in text:
            mode = 'shell'
        self.app.root.after(0, lambda: self.prompt_mode.set(mode))

    def rotate_log_if_needed(self):
        if self.log_fh and self.current_log_bytes >= CONFIG.get('log_max_bytes', 5*1024*1024):
            try:
                self.log_fh.close()
            except Exception:
                pass
            ts = time.strftime('%Y%m%d_%H%M%S')
            new_path = BASE_DIR / 'logs' / f"{self.name.replace(' ','_')}_{ts}_rotated.log"
            self.log_fh = open(new_path, 'a', encoding='utf-8', errors='ignore')
            self.current_log_bytes = 0
            self.safe_log(f"[{self.name}] Log rotated -> {new_path}")

    def safe_log(self, text, use_ansi=True):
        def _ins():
            txt = text
            if CONFIG.get('ansi_colors', True) and use_ansi:
                self.insert_with_ansi(txt)
            else:
                self.terminal.insert('end', txt)
            self.terminal.see('end')
            if self.auto_log.get() and self.log_fh:
                try:
                    b = txt.encode('utf-8', errors='ignore')
                    self.log_fh.write(txt)
                    self.log_fh.flush()
                    self.current_log_bytes += len(b)
                    self.rotate_log_if_needed()
                except Exception:
                    pass
            # Trim scrollback if needed
            max_lines = CONFIG.get('scrollback_limit', 8000)
            try:
                lines = int(self.terminal.index('end-1c').split('.')[0])
                excess = lines - max_lines
                if excess > 500:
                    self.terminal.delete('1.0', f'{excess}.0')
            except Exception:
                pass
        self.app.root.after(0, _ins)

    def insert_with_ansi(self, s: str):
        pos = 0
        current_tags = []
        for m in SGR_RE.finditer(s):
            if m.start() > pos:
                self.terminal.insert('end', s[pos:m.start()], tuple(current_tags))
            params = m.group(1)
            if params == '' or params == '0':
                current_tags = []
            else:
                for p in params.split(';'):
                    try:
                        code = int(p)
                        if code in ANSI_COLORS:
                            tag = f'sgr_{code}'
                            if tag not in current_tags:
                                current_tags.append(tag)
                        elif code == 39:
                            current_tags = [t for t in current_tags if not t.startswith('sgr_')]
                    except Exception:
                        pass
            pos = m.end()
        if pos < len(s):
            self.terminal.insert('end', s[pos:], tuple(current_tags))

    # ---- Terminal sizing ----
    def compute_terminal_size_chars(self):
        try:
            cw = self.term_font.measure('M') or 8
            ch = self.term_font.metrics('linespace') or 16
            w = max(self.terminal.winfo_width(), 200)
            h = max(self.terminal.winfo_height(), 100)
            cols = max((w - 20)//cw, 40)
            rows = max((h - 10)//ch, 10)
            return int(cols), int(rows)
        except Exception:
            return 120, 40

    def schedule_resize_pty(self):
        self.app.root.after(250, self.do_resize_pty)

    def do_resize_pty(self):
        cols, rows = self.compute_terminal_size_chars()
        if self.ssh_channel and not self.ssh_channel.closed:
            try:
                self.ssh_channel.resize_pty(width=cols, height=rows)
            except Exception:
                pass

    def clear_screen(self):
        self.terminal.delete('1.0', 'end')

    def rename_tab(self):
        name = simple_input(self.app.root, 'Rename Tab', 'New tab title:', default=self.name)
        if name:
            self.name = name
            self.app.update_tab_title(self, name)

    # ---- [CHANGE-1] Credentials UI helpers ----
    def on_toggle_default_creds(self):
        # If checked but defaults are missing, warn and keep manual mode active
        if self.use_default_creds.get() and not (USERNAME and PASSWORD):
            messagebox.showwarning('Default credentials', 'No default credentials found. Enter username/password manually.')
            self.use_default_creds.set(False)
        # Enable/disable manual fields
        state = 'disabled' if self.use_default_creds.get() else 'normal'
        try:
            self.user_entry.configure(state=state)
            self.pass_entry.configure(state=state)
        except Exception:
            pass

    # ---- Connect / Disconnect ----
    def connect_device(self):
        # Decide credentials for this tab
        if self.use_default_creds.get():
            if not (USERNAME and PASSWORD):
                messagebox.showerror('Error',f'Default credentials not loaded. Place credentials in:1) {CRED_PRIMARY} 2) {CRED_FALLBACK} Or uncheck "Use default credentials" and enter manually.')
                return
            u, p = USERNAME, PASSWORD
        else:
            u = (self.user_var.get() or '').strip()
            p = self.pass_var.get() or ''
            if not u or not p:
                messagebox.showerror('Error', 'Please enter Username and Password.')
                return

        host = (self.ip_var.get() or '').strip()
        proto = self.protocol_var.get().lower()
        if not host:
            messagebox.showerror('Error', 'Host is required.')
            return
        try:
            port = int(self.port_var.get() or '0')
        except Exception:
            port = 0
        if port <= 0:
            port = CONFIG.get('ssh_port' if proto == 'ssh' else 'telnet_port', 22 if proto == 'ssh' else 23)
            self.port_var.set(str(port))

        save_ip_to_history(host)
        self.app.refresh_ip_dropdowns()
        self.safe_log(f"[{self.name}] Connecting to {host}:{port} via {proto}...")
        self.update_status('Connecting...')
        self.terminal.focus_set()
        self.app.update_tab_title(self)
        self.open_log_file(host, proto)

        self.creds = (u, p)  # store chosen creds for worker threads
        self.connected = False

        if proto == 'ssh':
            threading.Thread(target=self._connect_ssh, args=(host, port), daemon=True).start()
        else:
            self._start_telnet_thread(host, port)

    def open_log_file(self, ip, proto):
        if not self.auto_log.get():
            self.close_log(); return
        ts = time.strftime('%Y%m%d_%H%M%S')
        safe_ip = (ip or self.name).replace(':', '_').replace('/', '_').replace(' ', '_')
        path = BASE_DIR / 'logs' / f'{safe_ip}_{proto}_{ts}.log'
        try:
            self.log_fh = open(path, 'a', encoding='utf-8', errors='ignore')
            self.current_log_bytes = 0
            self.safe_log(f'[{self.name}] Logging to: {path}')
        except Exception as e:
            self.safe_log(f'[{self.name}] Log open failed: {e}')
            self.log_fh = None

    def close_log(self):
        try:
            if self.log_fh: self.log_fh.close()
        except Exception:
            pass
        self.log_fh = None
        self.current_log_bytes = 0

    def disconnect(self):
        self.connected = False
        # SSH
        try:
            if self.ssh_channel and not self.ssh_channel.closed:
                self.ssh_channel.close()
        except Exception:
            pass
        try:
            if self.ssh_client:
                self.ssh_client.close()
        except Exception:
            pass
        # Telnet
        try:
            if self.telnet_writer:
                self.telnet_writer.close()
        except Exception:
            pass
        try:
            if self.telnet_loop:
                self.telnet_loop.call_soon_threadsafe(self.telnet_loop.stop)
        except Exception:
            pass
        self.update_status('Disconnected')
        self.safe_log(f'[{self.name}] Disconnected.')

    # ---- SSH ----
    def _connect_ssh(self, ip, port):
        try:
            import paramiko
        except ImportError:
            self.safe_log(f'[{self.name}] Paramiko not installed. pip install paramiko')
            self.update_status('Disconnected')
            return
        backoff = 2
        while True:
            try:
                self.ssh_client = paramiko.SSHClient()
                try:
                    self.ssh_client.load_system_host_keys()
                    if KNOWN_HOSTS_FILE.exists():
                        self.ssh_client.load_host_keys(str(KNOWN_HOSTS_FILE))
                    self.ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                except Exception:
                    self.ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                # Jump host via ProxyCommand (local OpenSSH)
                sock = None
                jmp = (self.jump_var.get() or '').strip()
                if jmp:
                    try:
                        from paramiko.proxy import ProxyCommand
                        sock = ProxyCommand(f"ssh -W {ip}:{port} {jmp}")
                    except Exception as e:
                        self.safe_log(f'[{self.name}] Jump host failed: {e}')
                u, p = getattr(self, 'creds', (USERNAME, PASSWORD))
                self.ssh_client.connect(ip, port=port, username=u, password=p,
                                        look_for_keys=False, allow_agent=False, timeout=15,
                                        sock=sock)
                trans = self.ssh_client.get_transport()
                if trans:
                    trans.set_keepalive(CONFIG.get('keepalive_seconds', 30))
                cols, rows = self.compute_terminal_size_chars()
                term_name = CONFIG.get('term_type', 'xterm')  # add 'term_type' to DEFAULT_CONFIG if you want to expose it
                self.ssh_channel = self.ssh_client.invoke_shell(term=term_name, width=cols, height=rows)

                self.ssh_channel.settimeout(0.0)
                self.connected = True
                self.update_status(f'Connected (SSH {ip})')
                self.safe_log(f'[{self.name}] SSH connected. Type directly.')
                if SEND_INIT_CMDS and INIT_COMMANDS:
                    def _send_init():
                        time.sleep(0.3)
                        for cmd in INIT_COMMANDS:
                            try:
                                self.ssh_channel.send(cmd + '\r')
                                time.sleep(0.12)
                            except Exception:
                                break
                    threading.Thread(target=_send_init, daemon=True).start()

                def read_output():
                    while self.ssh_channel and not self.ssh_channel.closed:
                        try:
                            if self.ssh_channel.recv_ready():
                                data = self.ssh_channel.recv(8192)
                                if not data:
                                    time.sleep(0.03); continue
                                text = data.decode(errors='ignore')
                                self.last_rx_time = time.time()
                                self.update_prompt_mode(text)
                                self.safe_log(text)
                            else:
                                time.sleep(0.03)
                        except Exception:
                            time.sleep(0.08)
                            continue
                self.reader_thread = threading.Thread(target=read_output, daemon=True)
                self.reader_thread.start()
                self.schedule_resize_pty()
                return
            except Exception as e:
                self.connected = False
                self.update_status('Disconnected')
                self.safe_log(f'[{self.name}] SSH connect failed: {e}')
                if not self.auto_reconnect.get():
                    return
                self.safe_log(f'[{self.name}] Retrying in {backoff}s...')
                time.sleep(backoff)
                backoff = min(backoff*2, 30)

    # ---- Telnet ----
    async def _telnet_connect_coro(self, ip, port):
        try:
            import telnetlib3
        except ImportError:
            self.safe_log(f'[{self.name}] telnetlib3 not installed. pip install telnetlib3')
            self.update_status('Disconnected')
            return
        try:
            self.telnet_reader, self.telnet_writer = await telnetlib3.open_connection(host=ip, port=port, encoding='utf8')
            self.connected = True
            self.update_status(f'Connected (Telnet {ip})')
            self.safe_log(f'[{self.name}] Telnet connected. Type directly.')

            u, p = getattr(self, 'creds', (USERNAME, PASSWORD))

            async def login_and_init():
                start = time.monotonic()
                while time.monotonic() - start < 12:
                    data = await self.telnet_reader.read(1024)
                    if not data:
                        break
                    self.last_rx_time = time.time()
                    self.safe_log(data)
                    low = data.lower()
                    if ('username' in low or 'login:' in low) and u:
                        self.telnet_writer.write(u + '')
                    elif 'password' in low and p:
                        self.telnet_writer.write(p + '')
                    if any(ch in low for ch in ['#', '>', '$']):
                        break
                if SEND_INIT_CMDS and INIT_COMMANDS:
                    for cmd in INIT_COMMANDS:
                        try:
                            self.telnet_writer.write(cmd + '')
                            await asyncio.sleep(0.12)
                        except Exception:
                            break
            await login_and_init()

            async def read_output():
                while True:
                    data = await self.telnet_reader.read(4096)
                    if not data:
                        break
                    self.last_rx_time = time.time()
                    self.update_prompt_mode(data)
                    self.safe_log(data)
            await read_output()
        except Exception as e:
            self.connected = False
            self.update_status('Disconnected')
            self.safe_log(f'[{self.name}] Telnet connect failed: {e}')
            if self.auto_reconnect.get():
                await asyncio.sleep(4)
                self.app.root.after(0, lambda: self.connect_device())

    def _start_telnet_thread(self, ip, port):
        if self.telnet_thread and self.telnet_thread.is_alive():
            self.safe_log(f'[{self.name}] Telnet loop already running.'); return
        def _runner():
            self.telnet_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.telnet_loop)
            self.telnet_loop.create_task(self._telnet_connect_coro(ip, port))
            try:
                self.telnet_loop.run_forever()
            finally:
                self.telnet_loop.close()
        self.telnet_thread = threading.Thread(target=_runner, daemon=True)
        self.telnet_thread.start()

    # ---- Input & Paste ----
    def send_to_remote(self, data: str):
        proto = self.protocol_var.get().lower()
        if proto == 'ssh' and self.ssh_channel and not self.ssh_channel.closed:
            try:
                self.ssh_channel.send(data)
            except Exception:
                pass
        elif proto == 'telnet' and self.telnet_writer and self.telnet_loop:
            try:
                self.telnet_loop.call_soon_threadsafe(self.telnet_writer.write, data)
            except Exception:
                pass

    def on_paste(self, event=None):
        try:
            text = self.app.root.clipboard_get() or ''
            if not text:
                return 'break'
            lines = text.replace('', '').replace('', '').split('')
            if len(lines) > 40:
                if not messagebox.askokcancel('Large paste', f'Send {len(lines)} lines?'):
                    return 'break'
            delay = CONFIG.get('paste_throttle_ms', 8)/1000.0
            for line in lines:
                self.send_to_remote(line + '')
                time.sleep(delay)
        except Exception:
            pass
        return 'break'

    def on_keypress(self, event):
        ctrl = (event.state & 0x4) != 0
        alt  = (event.state & 0x8) != 0
        if event.keysym == 'space':
            self.send_to_remote(' ')
            return 'break'
        if event.keysym in ('Return', 'KP_Enter'):
            self.send_to_remote('\r')
            return 'break'
        if event.keysym in ('BackSpace', 'KP_BackSpace'):
            self.send_to_remote(''); return 'break'
        if event.keysym == 'Delete':
            self.send_to_remote('[3~'); return 'break'
        arrows = { 'Up':'[A','Down':'[B','Right':'[C','Left':'[D' }
        if event.keysym in arrows:
            self.send_to_remote(arrows[event.keysym]); return 'break'
        specials = { 'Home':'[H','End':'[F','Prior':'[5~','Next':'[6~','Insert':'[2~','Tab':'	','Escape':'' }
        if event.keysym in specials:
            self.send_to_remote(specials[event.keysym]); return 'break'
        if ctrl and not alt:
            ks = event.keysym.lower()
            if ks == 'c': self.send_to_remote(''); return 'break'
            if ks == 'z': self.send_to_remote(''); return 'break'
            if ks == 'd': self.send_to_remote(''); return 'break'
            if ks == 'v': return self.on_paste(event)
            if ks == 'a': self.send_to_remote(''); return 'break'
            if ks == 'e': self.send_to_remote(''); return 'break'
        # Enter: commit current line to history
        if event.keysym in ('Return','KP_Enter'):
            eol = '\r\n' if CONFIG.get('enter_mode', 'CR') == 'CRLF' else '\r'
            self.send_to_remote(eol)
            line = ''.join(self.command_line_buf).strip()
            if line:
                self.history.append(line)
                self.history_global.append(line)
                if len(self.history_global) > 500:
                    del self.history_global[:len(self.history_global)-500]
            self.command_line_buf.clear()
            return 'break'
        # Backspace
        if event.keysym in ('BackSpace','KP_BackSpace'):
            bs = '\x7f' if CONFIG.get('backspace_mode', 'DEL') == 'DEL' else '\x08'
            self.send_to_remote(bs)
            if self.command_line_buf:
                self.command_line_buf.pop()
            return 'break'

        if event.char and len(event.char) == 1:
            self.send_to_remote(event.char); return 'break'
        return 'break'

    # ---- Buffer save/export ----
    def save_buffer_txt(self):
        content = self.terminal.get('1.0', 'end-1c')
        ts = time.strftime('%Y%m%d_%H%M%S')
        path = BASE_DIR / 'logs' / f'buffer_{self.name}_{ts}.txt'
        with open(path, 'w', encoding='utf-8', errors='ignore') as f:
            f.write(content)
        self.safe_log(f'[{self.name}] Buffer saved: {path}')

    def save_buffer_html(self):
        content = self.terminal.get('1.0', 'end-1c')
        ts = time.strftime('%Y%m%d_%H%M%S')
        path = BASE_DIR / 'logs' / f'buffer_{self.name}_{ts}.html'
        html = f"""<html><head><meta charset='utf-8'><style>
pre {{ white-space: pre-wrap; font: 12px Menlo, Consolas, monospace; background:#111; color:#eee; padding:8px; }}
</style></head><body><pre>{escape_html(content)}</pre></body></html>"""
        with open(path, 'w', encoding='utf-8', errors='ignore') as f:
            f.write(html)
        self.safe_log(f'[{self.name}] Buffer exported: {path}')

# ----------------------- Utility Dialogs -----------------------
from tkinter import simpledialog

def simple_input(root, title, prompt, default=''):
    return simpledialog.askstring(title, prompt, initialvalue=default, parent=root)

def escape_html(s: str) -> str:
    return (s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))

# ----------------------- App -----------------------
class App:
    def set_opacity(self):
        top = tk.Toplevel(self.root)
        top.title("Set Terminal Opacity")
        tk.Label(top, text="Opacity (0.3 to 1.0):").pack(padx=8, pady=4)
        opacity_var = tk.DoubleVar(value=self.root.attributes('-alpha'))
        scale = tk.Scale(top, from_=0.3, to=1.0, resolution=0.05, orient='horizontal', variable=opacity_var)
        scale.pack(padx=8, pady=4)
        def apply():
            self.root.attributes('-alpha', opacity_var.get())
            top.destroy()
        tk.Button(top, text="Apply", command=apply).pack(pady=8)
    def __init__(self):
        self.root = tk.Tk()
        self.root.title('MS-Term Pro — Multi-Tab SSH/Telnet')
        self.global_history = []

        # Menu bar
        menubar = tk.Menu(self.root)
        filem = tk.Menu(menubar, tearoff=0)
        filem.add_command(label='New Tab (Ctrl+T)', command=self.add_session)
        filem.add_command(label='Close Tab (Ctrl+W)', command=self.close_current_tab)
        filem.add_separator()
        filem.add_command(label='Open Session Folder', command=self.open_base_folder)
        filem.add_command(label='Settings…', command=self.open_settings)
        filem.add_separator(); filem.add_command(label='Exit', command=self.on_close)
        menubar.add_cascade(label='File', menu=filem)

        editm = tk.Menu(menubar, tearoff=0)
        editm.add_command(label='Find… (Ctrl+F)', command=self.find_current)
        editm.add_command(label='Rename Tab…', command=self.rename_current)
        menubar.add_cascade(label='Edit', menu=editm)

        viewm = tk.Menu(menubar, tearoff=0)
        viewm.add_command(label='Dark Mode', command=lambda: self.set_theme('dark'))
        viewm.add_command(label='Light Mode', command=lambda: self.set_theme('light'))
        viewm.add_separator()
        viewm.add_command(label='Font Settings…', command=lambda: self.get_current_session().open_font_dialog())
        viewm.add_command(label='Color Settings…', command=lambda: self.get_current_session().open_color_dialog())
        
        #viewm.add_command(label='Enter sends CRLF (toggle)', command=self.toggle_crlf)
        #viewm.add_command(label='Backspace=BS (toggle)', command=self.toggle_backspace)

        viewm.add_command(label='Font Bigger (Ctrl+=)', command=self.font_bigger)
        viewm.add_command(label='Font Smaller (Ctrl+-)', command=self.font_smaller)
        viewm.add_command(label='Font Reset (Ctrl+0)', command=self.font_reset)
        viewm.add_command(label='Set Terminal Opacity…', command=self.set_opacity)
        menubar.add_cascade(label='View', menu=viewm)

        toolsm = tk.Menu(menubar, tearoff=0)
        toolsm.add_command(label='Command Runner…', command=self.open_cmd_runner)
        toolsm.add_command(label='Broadcast Command…', command=self.broadcast_command)
        toolsm.add_command(label='Snippets…', command=self.open_snippets)
        toolsm.add_command(label='Notes (Current Tab)…', command=self.open_notes_current)
        toolsm.add_separator(); toolsm.add_command(label='Diff Two Files…', command=self.diff_files)
        menubar.add_cascade(label='Tools', menu=toolsm)
        self.root.config(menu=menubar)

        # Toolbar
        topbar = tk.Frame(self.root)
        topbar.grid(row=0, column=0, sticky='ew')
        tk.Button(topbar, text='+ New Tab', width=12, command=self.add_session).pack(side='left', padx=8, pady=6)
        tk.Button(topbar, text='× Close Tab', width=12, command=self.close_current_tab).pack(side='left', padx=(0, 8), pady=6)
        tk.Button(topbar, text='Open Folder', width=14, command=self.open_base_folder).pack(side='left', padx=(0, 8), pady=6)

        # Notebook
        self.notebook = ttk.Notebook(self.root)
        self.notebook.grid(row=1, column=0, sticky='nsew')

        # Status bar (app)
        self.app_status = tk.StringVar(value=f'Storage: {BASE_DIR}')
        tk.Label(self.root, textvariable=self.app_status, anchor='w').grid(row=2, column=0, sticky='ew', padx=8, pady=(0, 8))

        # Layout weights
        self.root.rowconfigure(0, weight=0)
        self.root.rowconfigure(1, weight=1)
        self.root.rowconfigure(2, weight=0)
        self.root.columnconfigure(0, weight=1)

        # First tab
        self.sessions = []
        self.session_counter = 0
        self.add_session()

        # Hotkeys
        self.root.bind('<Control-t>', lambda e: (self.add_session(), 'break'))
        self.root.bind('<Control-w>', lambda e: (self.close_current_tab(), 'break'))
        self.root.bind('<Control-Tab>', lambda e: (self.next_tab(), 'break'))
        self.root.bind('<Control-KeyPress-ISO_Left_Tab>', lambda e: (self.prev_tab(), 'break'))
        self.root.bind('<Control-F>', lambda e: (self.find_current(), 'break'))
        self.root.bind('<Control-Key-=>', lambda e: (self.font_bigger(), 'break'))
        self.root.bind('<Control-Key-minus>', lambda e: (self.font_smaller(), 'break'))
        self.root.bind('<Control-Key-0>', lambda e: (self.font_reset(), 'break'))

        # Apply theme
        self.apply_theme_all()

        # Close handler
        self.root.protocol('WM_DELETE_WINDOW', self.on_close)

    # ---- Theme & Fonts ----
    def apply_theme_all(self):
        if CONFIG.get('theme', 'light') == 'dark':
            self.root.configure(bg='#1e1e1e')
        else:
            self.root.configure(bg='#f0f0f0')
        for s in self.sessions:
            s.apply_theme()

    def set_theme(self, theme):
        CONFIG['theme'] = theme
        save_json(CONFIG_FILE, CONFIG)
        self.apply_theme_all()

    def font_bigger(self):
        CONFIG['font_size'] = min(CONFIG.get('font_size', 12) + 1, 24)
        save_json(CONFIG_FILE, CONFIG)
        for s in self.sessions:
            s.term_font.configure(size=CONFIG['font_size'])
            s.schedule_resize_pty()

    def font_smaller(self):
        CONFIG['font_size'] = max(CONFIG.get('font_size', 12) - 1, 8)
        save_json(CONFIG_FILE, CONFIG)
        for s in self.sessions:
            s.term_font.configure(size=CONFIG['font_size'])
            s.schedule_resize_pty()

    def font_reset(self):
        CONFIG['font_size'] = DEFAULT_CONFIG['font_size']
        save_json(CONFIG_FILE, CONFIG)
        for s in self.sessions:
            s.term_font.configure(size=CONFIG['font_size'])
            s.schedule_resize_pty()

    # ---- Sessions ----
    def add_session(self):
        self.session_counter += 1
        name = f'Session {self.session_counter}'
        sess = SessionTab(self, name)
        self.sessions.append(sess)
        self.notebook.add(sess.frame, text=name)
        self.notebook.select(sess.frame)
        sess.terminal.focus_set()

    def get_current_session(self):
        current = self.notebook.select()
        for s in self.sessions:
            if str(s.frame) == current:
                return s
        return None

    def close_current_tab(self):
        s = self.get_current_session()
        if not s:
            return
        idx = self.notebook.index(s.frame)
        s.disconnect(); s.close_log()
        self.notebook.forget(idx)
        try:
            self.sessions.remove(s)
        except Exception:
            pass
        if not self.sessions:
            self.add_session()

    def next_tab(self):
        idx = self.notebook.index('current')
        total = len(self.notebook.tabs())
        self.notebook.select((idx + 1) % total)

    def prev_tab(self):
        idx = self.notebook.index('current')
        total = len(self.notebook.tabs())
        self.notebook.select((idx - 1) % total)

    def update_tab_title(self, sess: 'SessionTab', title=None):
        if title is None:
            proto = sess.protocol_var.get()
            ip = (sess.ip_var.get() or sess.name)
            title = f"{ip} ({proto})"
        self.notebook.tab(sess.frame, text=title)

    def get_ip_history(self):
        return IP_HISTORY

    def refresh_ip_dropdowns(self):
        for s in self.sessions:
            try:
                s.ip_dropdown['values'] = IP_HISTORY
            except Exception:
                pass

    def open_base_folder(self):
        try:
            if os.name == 'nt':
                os.startfile(str(BASE_DIR))
            elif sys.platform == 'darwin':
                os.system(f'open "{BASE_DIR}"')
            else:
                os.system(f'xdg-open "{BASE_DIR}"')
        except Exception:
            messagebox.showinfo('Folder', f'Path: {BASE_DIR}')

    # ---- Tools ----
    def open_settings(self):
        top = tk.Toplevel(self.root); top.title('Settings')
        items = [
            ('Scrollback limit', 'scrollback_limit'),
            ('Timestamps (True/False)', 'timestamps'),
            ('ANSI colors (True/False)', 'ansi_colors'),
            ('Keepalive seconds', 'keepalive_seconds'),
            ('SSH port', 'ssh_port'),
            ('Telnet port', 'telnet_port'),
            ('Idle warn seconds', 'idle_warn_seconds'),
            ('Log max bytes', 'log_max_bytes'),
            ('Enter mode (CR or CRLF)', 'enter_mode'),
            ('Backspace mode (DEL or BS)', 'backspace_mode'),

            
        ]
        entries = {}
        for r, (label, key) in enumerate(items):
            ttk.Label(top, text=label).grid(row=r, column=0, sticky='w', padx=8, pady=4)
            ent = ttk.Entry(top); ent.insert(0, str(CONFIG.get(key)))
            ent.grid(row=r, column=1, sticky='ew', padx=8, pady=4)
            entries[key] = ent
        top.columnconfigure(1, weight=1)
        def save():
            for k, e in entries.items():
                v = e.get().strip()
                if v.lower() in ('true', 'false'):
                    CONFIG[k] = (v.lower() == 'true')
                else:
                    try:
                        CONFIG[k] = int(v)
                    except Exception:
                        CONFIG[k] = v
            save_json(CONFIG_FILE, CONFIG)
            self.apply_theme_all()
            messagebox.showinfo('Saved', 'Settings saved.')
        ttk.Button(top, text='Save', command=save).grid(row=len(items), column=1, sticky='e', padx=8, pady=8)

    def open_cmd_runner(self):
        s = self.get_current_session()
        if not s: return
        top = tk.Toplevel(self.root); top.title('Command Runner')
        tk.Label(top, text='Commands (one per line):').pack(anchor='w', padx=8, pady=(8,0))
        txt = tk.Text(top, width=80, height=20); txt.pack(fill='both', expand=True, padx=8, pady=8)
        delay_var = tk.StringVar(value='120')
        tk.Label(top, text='Delay (ms) between commands:').pack(anchor='w', padx=8)
        tk.Entry(top, textvariable=delay_var, width=8).pack(anchor='w', padx=8)
        def run():
            try:
                delay = max(0, int(delay_var.get())/1000.0)
            except Exception:
                delay = 0.12
            lines = [l.strip() for l in txt.get('1.0', 'end-1c').splitlines() if l.strip()]
            def worker():
                for cmd in lines:
                    s.send_to_remote(cmd + '')
                    time.sleep(delay)
            threading.Thread(target=worker, daemon=True).start()
        tk.Button(top, text='Run', command=run).pack(anchor='e', padx=8, pady=8)

    def broadcast_command(self):
        s = self.get_current_session()
        if not s: return
        cmd = simple_input(self.root, 'Broadcast', 'Send command to all connected sessions:')
        if not cmd: return
        for t in self.sessions:
            if t.connected:
                t.send_to_remote(cmd + '')

    def open_snippets(self):
        s = self.get_current_session()
        if not s: return
        items = [f"{i+1}. {sn['name']}" for i, sn in enumerate(SNIPPETS)]
        top = tk.Toplevel(self.root); top.title('Snippets')
        lb = tk.Listbox(top); lb.pack(fill='both', expand=True)
        for it in items: lb.insert('end', it)
        def ok():
            try:
                idx = lb.curselection()[0]
                text = SNIPPETS[idx]['text']
                s.send_to_remote(text + '')
                top.destroy()
            except Exception: pass
        tk.Button(top, text='Send', command=ok).pack(anchor='e', padx=8, pady=8)

    def diff_files(self):
        a = filedialog.askopenfilename(title='Select first file');
        if not a: return
        b = filedialog.askopenfilename(title='Select second file');
        if not b: return
        try:
            with open(a, 'r', encoding='utf-8', errors='ignore') as fa: A = fa.readlines()
            with open(b, 'r', encoding='utf-8', errors='ignore') as fb: B = fb.readlines()
            diff = difflib.unified_diff(A, B, fromfile=a, tofile=b)
            top = tk.Toplevel(self.root); top.title('Diff')
            txt = tk.Text(top, wrap='none'); txt.pack(fill='both', expand=True)
            txt.insert('1.0', ''.join(diff))
        except Exception as e:
            messagebox.showerror('Diff error', str(e))

    def open_notes_current(self):
        s = self.get_current_session()
        if not s: return
        ip = (s.ip_var.get() or s.name).replace(':', '_').replace('/', '_')
        path = BASE_DIR / 'sessions' / f'{ip}.md'
        text = ''
        try:
            if path.exists():
                with open(path, 'r', encoding='utf-8') as f: text = f.read()
        except Exception:
            pass
        top = tk.Toplevel(self.root); top.title(f'Notes — {ip}')
        txt = tk.Text(top, wrap='word'); txt.pack(fill='both', expand=True)
        txt.insert('1.0', text)
        def _save():
            try:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(txt.get('1.0', 'end-1c'))
                messagebox.showinfo('Saved', f'Saved to {path}')
            except Exception as e:
                messagebox.showerror('Error', str(e))
        tk.Button(top, text='Save', command=_save).pack(anchor='e', padx=8, pady=6)

    def find_current(self):
        pass  # (Find dialog wiring can be added back as needed)

    def rename_current(self):
        s = self.get_current_session()
        if s:
            s.rename_tab()

    # ---- App lifecycle ----
    def on_close(self):
        for s in list(self.sessions):
            try:
                s.disconnect(); s.close_log()
            except Exception:
                pass
        self.root.destroy()

    def run(self):
        first = self.sessions[0]
        first.safe_log('Welcome. Type directly. Right-click for menu.')
        self.root.mainloop()

# ----------------------- Main -----------------------
if __name__ == '__main__':
    App().run()
