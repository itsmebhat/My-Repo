import paramiko
import re
import time
import json
import os
import threading
import socket
from queue import Queue
from typing import List, Dict, Tuple, Optional
from openpyxl import Workbook

# =============================
# CONFIGURATION
# =============================
CREDENTIALS_FILE = r'C:\Users\pravek20\credentials_file.json'
OUTPUT_XLSX = r'C:\Users\pravek20\CDP_Traversal_Report.xlsx'
OUTPUT_CSV  = r'C:\Users\pravek20\CDP_Traversal_Report.csv'

MAX_WORKERS = 20
SSH_CONNECT_TIMEOUT = 30.0
SSH_AUTH_TIMEOUT = 45.0
SSH_BANNER_TIMEOUT = 60.0
READ_TIMEOUT = 20.0
CHANNEL_SLEEP = 0.05
KEEPALIVE_INTERVAL = 15
TCP_PRECHECK_TIMEOUT = 2.0

ONLY_VENDOR_CISCO = True  # Applies to CDP traversal/login only; LLDP is always included in report
WRITE_UNFILTERED_IF_EMPTY = True
ENABLE_PASSWORD: Optional[str] = None
PREFER_SSH = True
ALLOW_TELNET_FALLBACK = False

PRINT_LOCK = threading.Lock()

# =============================
# AP / WLC MODEL PATTERNS
# =============================
AP_MODEL_PATTERN  = re.compile(r'(?i)\bAIR[-_]AP\b')
WLC_MODEL_PATTERN = re.compile(r'(?i)\bAIR[-_]CT\d+\b')

# =============================
# UTILITIES
# =============================
_INVALID_XLS_SHEET_CHARS = r'[:\\/*?\[\]]'

def sanitize_sheet_title(title: str, default: str="Sheet") -> str:
    cleaned = re.sub(_INVALID_XLS_SHEET_CHARS, "-", title).strip().strip("'")
    return (cleaned or default)[:31]

def safe_print(msg: str):
    with PRINT_LOCK:
        print(msg, flush=True)

def is_port_open(host: str, port: int, timeout: float=TCP_PRECHECK_TIMEOUT) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False

def load_credentials(path: str) -> Dict[str,str]:
    with open(path, 'r') as f:
        return json.load(f)

def strip_ansi(text: str) -> str:
    ansi = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
    return ansi.sub('', text)

def parse_hostname(output: str) -> Optional[str]:
    m = re.search(r'(?mi)^\s*Hostname:\s*([A-Za-z0-9._\-\(\)/:]+)', output)
    if m: return m.group(1).strip()
    m = re.search(r'(?mi)^\s*hostname\s+([A-Za-z0-9._\-\(\)/:]+)', output)
    if m: return m.group(1).strip()
    lines = [ln for ln in output.splitlines() if ln.strip()]
    for ln in reversed(lines[-10:]):
        m = re.search(r'([A-Za-z0-9._\-\(\)/:]+)\s*[>#]\s*$', ln)
        if m: return m.group(1).strip()
    return None

def normalize_platform_to_model(pl: str) -> Tuple[bool,str]:
    m = re.search(r'Platform:\s*([^\r\n,]+)', pl, flags=re.IGNORECASE)
    if not m:
        return False, ""
    platform = m.group(1).strip()
    is_cisco = bool(re.search(r'\bcisco\b', platform, flags=re.IGNORECASE))
    model = re.sub(r'\bcisco\b', '', platform, flags=re.IGNORECASE).strip()
    return is_cisco, (model or platform)

def parse_capabilities(blk: str) -> tuple[list[str], str]:
    """Parse CDP Capabilities line, stopping at EOL."""
    cap_raw = ""
    caps: list[str] = []
    m = re.search(r'Capabilities:\s*([^\r\n]+)', blk, flags=re.IGNORECASE)
    if m:
        cap_raw = m.group(1).strip()
        mapping = {
            'router': 'router',
            'switch': 'switch',
            'igmp': 'igmp',
            'host': 'host',
            'phone': 'phone',
            'trans-bridge': 'ap',
            'source-route-bridge': 'bridge',
            'repeater': 'repeater',
        }
        parts = [p.strip().lower() for p in re.split(r'[\s,]+', cap_raw)]
        for p in parts:
            norm = mapping.get(p, p)
            if norm and norm not in caps:
                caps.append(norm)
        return caps, cap_raw
    return [], ""

def extract_neighbor_ip(blk: str) -> str:
    for pat in [
        r'\bIP address:\s*([0-9.]+)',
        r'\bIPv4 Address:\s*([0-9.]+)',
        r'Management address\(es\):.*?IP:\s*([0-9.]+)',
        r'Entry address\(es\):.*?IP address:\s*([0-9.]+)',
    ]:
        m = re.search(pat, blk, flags=re.IGNORECASE | re.DOTALL)
        if m:
            return m.group(1).strip()
    return ""

def parse_interfaces_from_block(blk: str) -> Tuple[str,str]:
    m = re.search(
        r'(?i)Interface:\s*([A-Za-z0-9/.\-]+)\s*,\s*Port ID.*?:\s*([A-Za-z0-9/.\-]+)',
        blk
    )
    return (m.group(1).strip(), m.group(2).strip()) if m else ("","")

def parse_cdp_neighbors_detail(output: str) -> List[Dict[str,str]]:
    text = strip_ansi(output)
    blocks = re.split(r'(?i)\bDevice ID:\s*', text)[1:]
    res: List[Dict[str, str]] = []
    for blk in blocks:
        idx = blk.find('\n')
        dev_id = blk[:idx].strip() if idx > 0 else blk.strip()
        ip_addr = extract_neighbor_ip(blk)
        local_if, nbr_port = parse_interfaces_from_block(blk)
        plat = ""
        m = re.search(r'(?mi)^\s*Platform:\s*.+$', blk)
        if m: plat = m.group(0).strip()
        is_cisco, model = normalize_platform_to_model(plat)
        caps, cap_raw = parse_capabilities(blk)
        res.append({
            "device_id": dev_id,
            "neighbor_ip": ip_addr,
            "local_interface": local_if,
            "neighbor_port": nbr_port,
            "platform_line": plat,
            "model": model,
            "is_cisco": is_cisco,
            "capabilities": caps,
            "capabilities_str": cap_raw,
            "protocol": "CDP",
        })
    return res

# ---------- LLDP Parsing ----------

def _lldp_get(lines: List[str], label: str) -> Optional[str]:
    # Flexible key search: handle minor wording variations
    # label examples: 'Local Intf', 'Local Port id', 'System Name', 'Chassis id', 'System Description'
    lab_re = re.compile(rf'(?i)^\s*{label}\s*:\s*(.+)$')
    for ln in lines:
        m = lab_re.search(ln)
        if m:
            return m.group(1).strip()
    return None

_LLDP_BLOCK_SEPS = re.compile(r'(?mi)^(?:-\s*){5,}$')  # lines of dashes

def _split_lldp_blocks(text: str) -> List[str]:
    # Primary: start of block indicated by 'Local Intf:' or 'Local Port id:'
    starts = [m.start() for m in re.finditer(r'(?mi)^\s*Local\s+(?:Intf|Port id)\s*:', text)]
    if starts:
        starts.append(len(text))
        return [text[starts[i]:starts[i+1]] for i in range(len(starts)-1)]
    # Fallback: split by dashed separators
    parts = [p for p in _LLDP_BLOCK_SEPS.split(text) if p.strip()]
    # Keep only chunks that look like LLDP blocks
    blocks = []
    for p in parts:
        if re.search(r'(?mi)\b(System Name|Chassis id|Port id)\b', p):
            blocks.append(p)
    return blocks

def parse_lldp_neighbors_detail(output: str) -> List[Dict[str, str]]:
    """Parse Cisco IOS/IOS-XE 'show lldp neighbors detail' output."""
    text = strip_ansi(output)
    blocks = _split_lldp_blocks(text)
    if not blocks:
        return []
    res: List[Dict[str, str]] = []
    for blk in blocks:
        lines = [ln for ln in blk.splitlines() if ln.strip()]
        # Fields (try multiple label variants)
        local_if = (
            _lldp_get(lines, 'Local Intf') or
            _lldp_get(lines, 'Local Port id') or
            ""
        )
        nbr_port = _lldp_get(lines, 'Port id') or _lldp_get(lines, 'Port ID') or ""
        system_name = _lldp_get(lines, 'System Name') or ""
        chassis_id  = _lldp_get(lines, 'Chassis id') or _lldp_get(lines, 'Chassis ID') or ""
        sys_desc    = _lldp_get(lines, 'System Description') or ""

        # Management address (if advertised)
        ip_addr = ""
        for pat in [
            r'(?mi)Management Address(?:es)?\s*:.*?(?:IPv4 Address|IP)\s*:\s*([0-9.]+)',
            r'(?mi)IP(?:v4)?\s*:\s*([0-9.]+)',
            r'(?mi)Management Address\s*:\s*([0-9.]+)'
        ]:
            m = re.search(pat, blk)
            if m:
                ip_addr = m.group(1).strip()
                break

        # Capabilities
        cap_raw = (
            _lldp_get(lines, 'Enabled Capabilities') or
            _lldp_get(lines, 'System Capabilities') or
            ""
        )
        caps: List[str] = []
        if cap_raw:
            # Cisco may use letters (e.g., B,R) or words. Keep both; normalize common words.
            mapping = {
                'bridge': 'switch',
                'router': 'router',
                'wlan': 'ap',
                'wlan access point': 'ap',
                'repeater': 'repeater',
                'station': 'host',
                'station only': 'host',
                'telephone': 'phone',
                'docsis': 'docsis',
                'docsis cable device': 'docsis',
            }
            parts = [p.strip().lower() for p in re.split(r'[\s,]+', cap_raw) if p.strip()]
            for p in parts:
                caps.append(mapping.get(p, p))
            # dedupe
            caps = list(dict.fromkeys(caps))

        # Vendor/model hints (best effort)
        is_cisco = bool(re.search(r'(?i)\bcisco\b', system_name)) or bool(re.search(r'(?i)\bcisco\b', sys_desc))
        model = ""
        if is_cisco:
            m = re.search(r'(?i)\b(C\d{3,4}[A-Z0-9\-]+|WS\-[A-Z0-9\-]+|ASR\-?\d+|N9K\-[A-Z0-9\-]+)\b', sys_desc)
            if m:
                model = m.group(1)

        dev_id = system_name or chassis_id or ""
        res.append({
            "device_id": dev_id,
            "neighbor_ip": ip_addr,
            "local_interface": local_if,
            "neighbor_port": nbr_port,
            "platform_line": f"System Description: {sys_desc}" if sys_desc else "",
            "model": model,
            "is_cisco": is_cisco,
            "capabilities": caps,
            "capabilities_str": cap_raw,
            "protocol": "LLDP",
        })
    return res

# ---------- Channel helpers ----------

def _read_until_prompt(chan, timeout: float = READ_TIMEOUT, allow_more: bool = True) -> str:
    end = time.time() + timeout
    buf = ""
    while time.time() < end:
        if chan.recv_ready():
            chunk = chan.recv(65535).decode(errors='ignore')
            buf += chunk
            if allow_more and '--More--' in chunk:
                chan.send(' ')
                continue
            for ln in buf.splitlines()[-4:]:
                if ln.strip().endswith('#') or ln.strip().endswith('>'):
                    return buf
        else:
            time.sleep(CHANNEL_SLEEP)
    return buf

def _ssh_enable_if_needed(chan, enable_password: Optional[str]) -> None:
    if not enable_password:
        return
    chan.send("\n"); time.sleep(0.2)
    out = _read_until_prompt(chan, READ_TIMEOUT)
    if out.strip().endswith('>'):
        chan.send("enable\n"); time.sleep(0.2)
        out2 = _read_until_prompt(chan, READ_TIMEOUT)
        if re.search(r'assword:', out2, flags=re.IGNORECASE):
            chan.send(enable_password + "\n")
            time.sleep(0.2)
            _read_until_prompt(chan, READ_TIMEOUT)

def run_commands_over_ssh(ip: str, user: str, pwd: str,
                          cmds: List[str], enable_pwd: Optional[str]) -> Tuple[bool,str]:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    chan = None
    try:
        client.connect(
            ip,
            username=user, password=pwd,
            timeout=SSH_CONNECT_TIMEOUT,
            auth_timeout=SSH_AUTH_TIMEOUT,
            banner_timeout=SSH_BANNER_TIMEOUT,
            look_for_keys=False, allow_agent=False
        )
        tr = client.get_transport()
        if tr:
            try: tr.set_keepalive(KEEPALIVE_INTERVAL)
            except Exception: pass
            banner = tr.get_banner()
            if banner:
                safe_print(f"[INFO] Auth banner: {banner!r}")

        chan = client.invoke_shell(term='vt100', width=200, height=60)
        chan.settimeout(10)
        _ = _read_until_prompt(chan, READ_TIMEOUT)
        _ssh_enable_if_needed(chan, enable_pwd)
        chan.send("terminal length 0\n")
        _ = _read_until_prompt(chan, READ_TIMEOUT)

        full = ""
        for c in cmds:
            chan.send(c + "\n")
            out = _read_until_prompt(chan, READ_TIMEOUT)
            full += f"\n\n$ {c}\n{out}"
        try:
            chan.close()
        except Exception:
            pass
        client.close()
        return True, full
    except Exception as e:
        try:
            chan.close()
        except Exception:
            pass
        try:
            client.close()
        except Exception:
            pass
        return False, f"[SSH ERROR] {ip}: {e}"

class TraversalState:
    def __init__(self, user: str, pwd: str):
        self.username = user
        self.password = pwd
        self.queue = Queue()
        self.visited = set()
        self.visited_lock = threading.Lock()
        # (parent_host, parent_ip, local_if, nbr_if, nbr_ip, model, nbr_host, capabilities_str, protocol)
        self.results: List[Tuple[str,str,str,str,str,str,str,str,str]] = []
        self.errors:  List[Tuple[str,str,str]] = []
        self.results_lock = threading.Lock()
        self.errors_lock = threading.Lock()
        self.edge_dedupe = set()
        self.counters = {"queued":0, "completed":0, "failed":0}

    def add_neighbors_to_queue(self, ips: List[str]):
        for nip in ips:
            if not nip:
                continue
            with self.visited_lock:
                if nip in self.visited:
                    continue
                self.visited.add(nip)
            self.queue.put(nip)
            self.counters["queued"] += 1
            safe_print(f"[ENQUEUE] {nip} Queue size: {self.queue.qsize()}")

    def record_error(self, ip: str, err: str, hostname: str="Unknown"):
        with self.errors_lock:
            self.errors.append((ip, hostname, err))
            self.counters["failed"] += 1
        safe_print(f"[ERROR] {ip} ({hostname}) -> {err}")

    def record_result(self, row: Tuple[str,str,str,str,str,str,str,str,str]):
        # parent_ip, local_if, nbr_ip, nbr_if, protocol as the dedupe key
        key = (row[1], row[2], row[4], row[3], row[8])
        with self.results_lock:
            if key not in self.edge_dedupe:
                self.edge_dedupe.add(key)
                self.results.append(row)
                safe_print("\t".join(row))

def is_login_candidate(n: Dict[str,str]) -> bool:
    """Login only for CDP neighbors with caps S I or R S I."""
    if n.get("protocol") != "CDP":
        return False
    caps = set((n.get("capabilities") or []))
    return caps == {"switch", "igmp"} or caps == {"router", "switch", "igmp"}

def is_allowed_neighbor(n: Dict[str,str]) -> bool:
    """
    Include all LLDP neighbors in the report.
    For CDP, if ONLY_VENDOR_CISCO=True, require Cisco vendor for inclusion.
    """
    if n.get("protocol") == "LLDP":
        return True
    if ONLY_VENDOR_CISCO and not n.get("is_cisco"):
        return False
    return True

def _get_section(haystack: str, marker: str, next_markers: List[str]) -> str:
    lower = haystack.lower()
    start = lower.find(marker.lower())
    if start < 0:
        return ""
    ends = [lower.find(nm.lower(), start+1) for nm in next_markers]
    ends = [p for p in ends if p > 0]
    end = min(ends) if ends else len(haystack)
    return haystack[start:end]

def process_device(ip: str, st: TraversalState):
    start = time.time()
    ssh_open = is_port_open(ip, 22)
    telnet_open = is_port_open(ip, 23)

    ok = False
    out = ""
    via = "SSH"

    cmds = [
        "show hostname",
        "show running-config | include ^hostname",
        "show cdp neighbors detail",
        "show lldp neighbors detail",
    ]

    if PREFER_SSH and ssh_open:
        safe_print(f"[CONNECT] {ip} -> SSH")
        ok, out = run_commands_over_ssh(ip, st.username, st.password, cmds, ENABLE_PASSWORD)
        if not ok and ALLOW_TELNET_FALLBACK and telnet_open:
            safe_print(f"[CONNECT] {ip} -> SSH failed; Telnet fallback is enabled but not implemented in this script.")
    else:
        if ALLOW_TELNET_FALLBACK and telnet_open:
            safe_print(f"[CONNECT] {ip} -> Telnet (not implemented)")
            via = "Telnet"
        else:
            st.record_error(ip, "No mgmt port open")
            return

    if not ok:
        st.record_error(ip, out)
        return

    text = strip_ansi(out)
    hostname = parse_hostname(text) or f"Unknown({ip})"

    cdp_sec  = _get_section(text, "$ show cdp neighbors detail", ["$ show lldp"])
    lldp_sec = _get_section(text, "$ show lldp neighbors detail", [])

    cdp_neighbors  = parse_cdp_neighbors_detail(cdp_sec) if cdp_sec else []
    lldp_neighbors = parse_lldp_neighbors_detail(lldp_sec) if lldp_sec else []

    safe_print(f"[DEBUG RAW] {ip} ({hostname}) -> CDP:{len(cdp_neighbors)} LLDP:{len(lldp_neighbors)}")
    for n in cdp_neighbors + lldp_neighbors:
        caps_disp = n.get('capabilities_str','') or n.get('capabilities', [])
        caps_disp = " ".join(caps_disp.split()) if isinstance(caps_disp, str) else caps_disp
        safe_print(
            f"    -> [{n.get('protocol','?')}] id={n['device_id']}, ip={n['neighbor_ip']}, "
            f"model={n['model']}, caps={caps_disp}"
        )

    all_neighbors = [n for n in (cdp_neighbors + lldp_neighbors) if is_allowed_neighbor(n)]
    to_enqueue: List[str] = []

    for n in all_neighbors:
        nip   = n.get("neighbor_ip", "")
        l_if  = n.get("local_interface", "")
        n_if  = n.get("neighbor_port", "")
        model = n.get("model", "")
        nhost = n.get("device_id", "")
        caps_str = n.get("capabilities_str", "") or ", ".join(n.get("capabilities", []))
        protocol = n.get("protocol", "?")

        st.record_result((hostname, ip, l_if, n_if, nip, model, nhost, caps_str, protocol))

        # Enqueue ONLY CDP candidates; never enqueue LLDP devices
        if nip and is_login_candidate(n):
            to_enqueue.append(nip)
        else:
            safe_print(f"[SKIP LOGIN] {hostname} -> {nhost} ({caps_str}) via {protocol}")

    safe_print(f"[DEBUG ENQ ] {ip} will enqueue: {to_enqueue}")
    if to_enqueue:
        st.add_neighbors_to_queue(to_enqueue)

    st.counters["completed"] += 1
    safe_print(f"[DONE] {ip} ({hostname}) via {via}  time: {time.time()-start:.1f}s")

def worker(st: TraversalState):
    while True:
        ip = st.queue.get()
        if ip is None:
            st.queue.task_done()
            break
        try:
            process_device(ip, st)
        except Exception as e:
            st.record_error(ip, f"Exception: {e}")
        finally:
            st.queue.task_done()

def write_excel(rows: List[Tuple[str,str,str,str,str,str,str,str,str]],
                errors: List[Tuple[str,str,str]],
                path: str):
    wb = Workbook()
    ws = wb.active
    ws.title = sanitize_sheet_title("CDP_Map")
    ws.append([
        "Parent Hostname","Parent IP","Local If","Nbr If",
        "Nbr IP","Nbr Model","Nbr Hostname","Capabilities","Protocol"
    ])
    for r in rows:
        ws.append(list(r))
    if errors:
        ws2 = wb.create_sheet(sanitize_sheet_title("Failures"))
        ws2.append(["Device IP","Hostname","Error"])
        for ip, hn, err in errors:
            ws2.append([ip, hn, err])
    wb.save(path)

def write_csv(rows: List[Tuple[str,str,str,str,str,str,str,str,str]], path: str):
    import csv
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow([
            "Parent Hostname","Parent IP","Local If","Nbr If",
            "Nbr IP","Nbr Model","Nbr Hostname","Capabilities","Protocol"
        ])
        for r in rows:
            w.writerow(list(r))

def main():
    creds = load_credentials(CREDENTIALS_FILE)
    user = creds.get("username")
    pwd  = creds.get("password")
    if not user or not pwd:
        print("ERROR: credentials_file.json must have 'username' & 'password'")
        return

    seed = input("Enter starting device IP: ").strip()
    if not seed:
        print("ERROR: no seed IP provided.")
        return

    st = TraversalState(user, pwd)
    with st.visited_lock:
        st.visited.add(seed)
    st.queue.put(seed)
    st.counters["queued"] = 1

    threads = []
    for _ in range(MAX_WORKERS):
        t = threading.Thread(target=worker, args=(st,), daemon=True)
        t.start()
        threads.append(t)

    safe_print(f"[START] Seed: {seed}  workers: {MAX_WORKERS}  Cisco-only={ONLY_VENDOR_CISCO}")
    t0 = time.time()
    st.queue.join()

    for _ in threads:
        st.queue.put(None)
    for t in threads:
        t.join()

    duration = time.time() - t0
    rows_sorted = sorted(st.results, key=lambda x:(x[1], x[2], x[4], x[8]))

    os.makedirs(os.path.dirname(OUTPUT_XLSX), exist_ok=True)
    os.makedirs(os.path.dirname(OUTPUT_CSV),  exist_ok=True)
    write_excel(rows_sorted, st.errors, OUTPUT_XLSX)
    write_csv(rows_sorted, OUTPUT_CSV)

    print("\n===== SUMMARY =====")
    print(f"Queued:    {st.counters['queued']}")
    print(f"Completed: {st.counters['completed']}")
    print(f"Failed:    {st.counters['failed']}")
    print(f"Edges:     {len(rows_sorted)}")
    print(f"Errors:    {len(st.errors)}")
    print(f"Elapsed:   {duration:.1f}s")
    print(f"Excel:     {OUTPUT_XLSX}")
    print(f"CSV:       {OUTPUT_CSV}")

    if st.errors:
        print("\n[FAILED DEVICES]")
        for ip, hn, err in st.errors:
            print(f"- {ip} ({hn}): {err}")

if __name__ == "__main__":
    main()
