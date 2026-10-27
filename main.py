import os
import sys
import json
import time
import glob
import shutil
import requests
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from flask import Flask, render_template, request, jsonify   # ← NEW

# Termux UI-এর জন্য Rich Console সেটআপ
console = Console()

# ফোল্ডার ও ফাইল কনফিগারেশন
INPUT_FILE = "accounts.txt"
OUTPUT_FOLDER = "Results"

SUCCESS_FILE = f"{OUTPUT_FOLDER}/Valid_Accounts.txt"
DETAILS_FILE = f"{OUTPUT_FOLDER}/Details_Accounts.txt"
FAILED_FILE = f"{OUTPUT_FOLDER}/Failed_Accounts.txt"

THREAD_LIMIT = 4

# ফাইল রাইট থ্রেড-সেফ করার জন্য লক এবং ট্র্যাকার
FILE_LOCK = threading.Lock()

# API এন্ডপয়েন্টস
JWT_GEN_API = "http://78.154.103.23:12698/"
INFO_API = "http://78.154.103.39:11168/info"

PANEL_RESULTS_DIR = "Results"
PORT = int(os.environ.get("PORT", 5000))

# ✅ ঠিক করা ফিল্টার লজিক (ওভারল্যাপিং ছাড়া)
PANEL_FILTERS = {
    # Range Filters
    "92-100": lambda lvl: lvl >= 92,
    "72-91":  lambda lvl: 72 <= lvl <= 91,
    "52-71":  lambda lvl: 52 <= lvl <= 71,
    "32-51":  lambda lvl: 32 <= lvl <= 51,
    "22-31":  lambda lvl: 22 <= lvl <= 31,
    "8-21":   lambda lvl: 8 <= lvl <= 21,
    "3-7":    lambda lvl: lvl < 8,
    
    # Specific Exact Level Filters
    "8":      lambda lvl: lvl == 8,
    "10":     lambda lvl: lvl == 10,
    "20":     lambda lvl: lvl == 20,
    "30":     lambda lvl: lvl == 30,
    "40":     lambda lvl: lvl == 40,
    "50":     lambda lvl: lvl == 50,
    "60":     lambda lvl: lvl == 60,
    "70":     lambda lvl: lvl == 70,
    "80":     lambda lvl: lvl == 80,
    "90":     lambda lvl: lvl == 90,
    "100":    lambda lvl: lvl == 100,
}

PANEL_FILTER_LABELS = {
    "92-100": "92–100 Level",
    "72-91":  "72–91 Level",
    "52-71":  "52–71 Level",
    "32-51":  "32–51 Level",
    "22-31":  "22–31 Level",
    "8-21":   "8–21 Level",
    "3-7":    "3–7 Level",
    
    "8":      "Only Lvl 8",
    "10":     "Only Lvl 10",
    "20":     "Only Lvl 20",
    "30":     "Only Lvl 30",
    "40":     "Only Lvl 40",
    "50":     "Only Lvl 50",
    "60":     "Only Lvl 60",
    "70":     "Only Lvl 70",
    "80":     "Only Lvl 80",
    "90":     "Only Lvl 90",
    "100":    "Only Lvl 100",
}

# ✅ ডাইনামিকভাবে CLI-র জন্য ফাইল লিস্ট তৈরি
ALL_RESULT_FILES = [SUCCESS_FILE, DETAILS_FILE, FAILED_FILE]
for f in PANEL_FILTERS:
    ALL_RESULT_FILES.append(f"{OUTPUT_FOLDER}/{f}-lvl_Accounts.txt")

EXISTING_UIDS_PER_FILE = {fp: set() for fp in ALL_RESULT_FILES}
HEADER_WRITTEN_IN_SESSION = set()

PANEL_FILE_LOCK = threading.Lock()

PANEL_STATE = {
    "running": False,
    "done": True,
    "progress": 0,
    "total": 0,
    "current_region": None,
    "current_account": None,
    "counters": {f: 0 for f in PANEL_FILTERS},
    "failed": 0,
    "log": [],
    "per_region": {},
}

def setup_folders():
    """ফোল্ডার ও ফাইল তৈরি করা"""
    if not os.path.exists(OUTPUT_FOLDER):
        os.makedirs(OUTPUT_FOLDER)
    if not os.path.exists(INPUT_FILE):
        with open(INPUT_FILE, "w", encoding="utf-8") as f:
            f.write("# Enter accounts as UID:PASSWORD\n")
            f.write("6180106694:1_DEV_w8w1Ia3tpo6b\n")
        console.print(f"[bold yellow][!] '{INPUT_FILE}' file banano hoyeche. Data add kore abr run korun.[/bold yellow]")
        sys.exit()

def load_existing_uids_from_files():
    """ফাইলগুলোতে আগে থেকে থাকা সব UID মেমোরিতে লোড করা (ডুপ্লিকেট আটকানোর জন্য)"""
    for file_path in ALL_RESULT_FILES:
        if os.path.exists(file_path):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        
                        # UID বের করার লজিক (UID:Password অথবা UID: 123456 ইত্যাদি)
                        potential_uid = ""
                        if ":" in line:
                            first_part = line.split(":", 1)[0].strip()
                            clean = first_part.replace("UID", "").replace("uid", "").strip()
                            if clean.isdigit():
                                potential_uid = clean
                        elif "UID" in line:
                            parts = line.split("|")[0]
                            clean = parts.replace("UID:", "").replace("uid:", "").strip()
                            if clean.isdigit():
                                potential_uid = clean

                        if potential_uid:
                            EXISTING_UIDS_PER_FILE[file_path].add(potential_uid)
            except Exception:
                pass

def get_account_id(account_line):
    """নতুন API (status: success, real_uid) থেকে ডাটা সংগ্রহ"""
    headers = {"User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36"}
    line = account_line.strip()
    
    if ":" not in line:
        if line.isdigit():
            return {"account_id": line, "original_uid": line, "password": "", "error": None}
        return {"account_id": None, "original_uid": None, "password": None, "error": "Invalid format (UID:PASS chai)"}

    try:
        uid, password = line.split(":", 1)
        uid = uid.strip()
        password = password.strip()
    except ValueError:
        return {"account_id": None, "original_uid": None, "password": None, "error": "Parsing failed"}

    # আপনার API এন্ডপয়েন্ট ও প্যারামিটার
    api_url = "http://78.154.103.23:12698/token"
    params = {"uid": uid, "password": password}

    try:
        response = requests.get(api_url, params=params, headers=headers, timeout=15)
        if response.status_code == 200:
            res_json = response.json()
            
            # 🔥 নতুন JSON রেসপন্স অনুযায়ী চেক (status: "success" এবং real_uid / account_id)
            if res_json.get("status") == "success" or res_json.get("success") is True:
                # real_uid বা account_id অথবা uid খুঁজল
                acc_id = res_json.get("real_uid") or res_json.get("account_id") or res_json.get("uid")
                if acc_id:
                    return {"account_id": str(acc_id), "original_uid": uid, "password": password, "error": None}
                return {"account_id": None, "original_uid": None, "password": None, "error": "Real UID missing in response"}
            else:
                err_msg = res_json.get("message") or res_json.get("error") or "Login Rejected"
                return {"account_id": None, "original_uid": None, "password": None, "error": err_msg}
        return {"account_id": None, "original_uid": None, "password": None, "error": f"HTTP {response.status_code}"}
    except Exception as e:
        return {"account_id": None, "original_uid": None, "password": None, "error": f"Net Error: {str(e)}"}

def get_account_info(account_id):
    """API 2 থেকে ডাটা সংগ্রহ"""
    params = {"uid": account_id}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        response = requests.get(INFO_API, params=params, headers=headers, timeout=12)
        if response.status_code == 200:
            res_json = response.json()
            if any(k in res_json for k in ("BasicInformation", "basicInfo", "AccountInfo")):
                return {"data": res_json, "error": None}
            return {"data": None, "error": "Invalid Data format"}
        return {"data": None, "error": f"HTTP {response.status_code}"}
    except Exception as e:
        return {"data": None, "error": f"Net Error: {str(e)}"}

def save_to_file_safely(file_path, uid_key, content_line):
    """
    ফাইলে সেভ করার ফাংশন:
    ১. অলরেডি ফাইলে থাকলে ডুপ্লিকেট সেভ করবে না।
    ২. নতুন অ্যাকাউন্ট হলে সেশনের শুরুতে # --- New Accounts... হেডার দিবে।
    """
    with FILE_LOCK:
        # ফাইল যদি আগে স্ক্যান না হয়ে থাকে
        if file_path not in EXISTING_UIDS_PER_FILE:
            EXISTING_UIDS_PER_FILE[file_path] = set()

        # যদি এই ফাইলে অ্যাকাউন্টটি আগেই থাকে, তাহলে ডুপ্লিকেট করবে না
        if uid_key in EXISTING_UIDS_PER_FILE[file_path]:
            return False

        # নতুন অ্যাকাউন্ট পাওয়ার পর সেশনে প্রথমবার হেডার লেখা
        if file_path not in HEADER_WRITTEN_IN_SESSION:
            HEADER_WRITTEN_IN_SESSION.add(file_path)
            now_time = datetime.now().strftime("%d-%b-%Y %I:%M %p")
            header = f"\n# --- New Accounts Added on {now_time} ---\n"
            if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
                header = f"# --- New Accounts Added on {now_time} ---\n"
            with open(file_path, "a", encoding="utf-8") as f:
                f.write(header)

        # নতুন ডাটা অ্যাপেন্ড করা
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(content_line + "\n")

        EXISTING_UIDS_PER_FILE[file_path].add(uid_key)
        return True

def run_diagnostics(test_account):
    """প্রথম অ্যাকাউন্টের ওপর টেস্ট রান"""
    console.print("[bold yellow][⚡] SYSTEM DIAGNOSTIC CHALANO HOCHCHE...[/bold yellow]")
    test_account = test_account.strip()
    console.print(f"[cyan][*] Loaded Account:[/cyan] '{test_account}'")
    
    jwt_res = get_account_id(test_account)
    account_id = jwt_res["account_id"]
    if not account_id:
        console.print(f"[red][-] API 1 Fail hoyeche: {jwt_res['error']}[/red]")
        return False
    console.print(f"[green][+] API 1 Success! Get ID: {account_id}[/green]")

    info_res = get_account_info(account_id)
    if not info_res["data"]:
        console.print(f"[red][-] API 2 Fail hoyeche: {info_res['error']}[/red]")
        return False
        
    console.print("[bold green][👑] DIAGNOSTIC PASSED! System full active ও ready.[/bold green]\n")
    return True

def process_account(account_data):
    """প্রতিটি অ্যাকাউন্টের প্রসেসিং লজিক"""
    account_data = account_data.strip()
    if not account_data or account_data.startswith("#"):
        return None

    # ১. এপিআই ১ কল
    jwt_res = get_account_id(account_data)
    account_id = jwt_res["account_id"]
    original_uid = jwt_res["original_uid"]
    password = jwt_res["password"]
    
    if not account_id:
        failed_uid = original_uid if original_uid else account_data
        save_to_file_safely(FAILED_FILE, failed_uid, f"API1_ERR | Input: {account_data} | Details: {jwt_res['error']}")
        return {"status": "failed", "data": account_data, "reason": jwt_res["error"]}

    # ২. এপিআই ২ কল
    info_res = get_account_info(account_id)
    info_data = info_res["data"]
    
    if not info_data:
        save_to_file_safely(FAILED_FILE, account_id, f"API2_ERR | ID: {account_id} | Details: {info_res['error']}")
        return {"status": "failed", "data": account_data, "reason": info_res["error"]}

    # ডাটা পার্সিং
    if "BasicInformation" in info_data:
        basic_info = info_data.get("BasicInformation", {})
        nickname = basic_info.get("Name", "Unknown")
        level = basic_info.get("Level", 0)
        region = basic_info.get("Region", "Unknown")
    elif "basicInfo" in info_data:
        basic_info = info_data.get("basicInfo", {})
        nickname = basic_info.get("nickname", "Unknown")
        level = basic_info.get("level", 0)
        region = basic_info.get("region", "Unknown")
    elif "AccountInfo" in info_data:
        basic_info = info_data.get("AccountInfo", {})
        nickname = basic_info.get("AccountName", "Unknown")
        level = basic_info.get("AccountLevel", 0)
        region = basic_info.get("AccountRegion", "Unknown")
    else:
        nickname = "Unknown"
        level = 0
        region = "Unknown"

    try:
        lvl_val = int(level)
    except (ValueError, TypeError):
        lvl_val = 0

    # ইউনিক কী
    uid_key = original_uid if original_uid else account_id
    credentials_line = f"{original_uid}:{password}" if original_uid and password else f"{uid_key}"

    # ১. ভ্যালিড অ্যাকাউন্ট ফাইল
    save_to_file_safely(SUCCESS_FILE, uid_key, f"UID: {account_id} | Name: {nickname} | Level: {lvl_val} | Region: {region}")

    # ২. ডিটেইলস ফাইল
    save_to_file_safely(DETAILS_FILE, uid_key, f"{credentials_line} | Level: {lvl_val} | Name: {nickname}")

    # ৩. লেভেল ভিত্তিক ক্যাটাগরি ফিল্টারিং (ডাইনামিক)
    cat_tags = []
    for fk, condition in PANEL_FILTERS.items():
        if condition(lvl_val):
            cat_tags.append(fk)
            file_path = f"{OUTPUT_FOLDER}/{fk}-lvl_Accounts.txt"
            save_to_file_safely(file_path, uid_key, credentials_line)

    cat_tag = ", ".join(cat_tags) if cat_tags else "Unknown"

    return {
        "status": "success",
        "account_id": account_id,
        "nickname": nickname,
        "level": lvl_val,
        "region": region,
        "category": cat_tag
    }

def main():
    os.system("clear")
    
    banner = f"""[bold magenta]
    ███╗   ███╗    ███████╗ █████╗ ██████╗ ██╗  ██╗███████╗██████╗ 
    ████╗ ████║    ██╔════╝██╔══██╗██╔══██╗██║ ██╔╝██╔════╝██╔══██╗
    ██╔████╔██║    ███████╗███████║██████╔╝█████╔╝ █████╗  ██████╔╝
    ██║╚██╔╝██║    ╚════██║██╔══██║██╔══██╗██╔═██╗ ██╔══╝  ██╔══██╗
    ██║ ╚═╝ ██║██╗ ███████║██║  ██║██║  ██║██║  ██╗███████╗██║  ██║
    ╚═╝     ╚═╝╚═╝ ╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝[/bold magenta]
    
 [bold cyan]┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓[/bold cyan]
 [bold cyan]┃[/bold cyan] [bold yellow]✨ FF LEVEL FILTER ✨[/bold yellow]             [bold cyan]┃[/bold cyan]
 [bold cyan]┃[/bold cyan] [bold white]Developer: M. SARKER                              [/bold white] [bold cyan]┃[/bold cyan]
 [bold cyan]┃[/bold cyan] [bold white]Logic    : 22+ | 20-21 | 8-19 | <8 Levels            [/bold white] [bold cyan]┃[/bold cyan]
 [bold cyan]┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛[/bold cyan]
    """
    console.print(banner)

    setup_folders()
    load_existing_uids_from_files()

    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        raw_lines = [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]

    # accounts.txt থেকে ডুপ্লিকেট অ্যাকাউন্ট ফিল্টার করে সিঙ্গেল করা
    seen_uids_in_input = set()
    accounts_to_check = []

    for line in raw_lines:
        uid_part = line.split(":", 1)[0].strip() if ":" in line else line.strip()
        if uid_part not in seen_uids_in_input:
            seen_uids_in_input.add(uid_part)
            accounts_to_check.append(line)

    duplicate_removed = len(raw_lines) - len(accounts_to_check)
    if duplicate_removed > 0:
        console.print(f"[bold yellow][*] Input file theke {duplicate_removed} ti duplicate account remove kora hoyeche.[/bold yellow]")

    total_accounts = len(accounts_to_check)
    if total_accounts == 0:
        console.print(f"[red][!] '{INPUT_FILE}' file faka! Account add korun.[/red]")
        return

    # ডায়াগনস্টিক চেক
    diagnostic_success = run_diagnostics(accounts_to_check[0])
    if not diagnostic_success:
        console.print("[yellow][!] Diagnostic fail hoyeche. Server problem hote pare.[/yellow]\n")
        choice = input("Force check chalate chan? (y/n): ")
        if choice.lower() != 'y':
            sys.exit()

    console.print(f"[yellow][*] Total {total_accounts} ti unique account verify shuru hochche...[/yellow]\n")

    # লাইভ টেবিল ভিউ
    table = Table(title="Audited Live Accounts Info", border_style="cyan")
    table.add_column("Account ID", style="cyan", no_wrap=True)
    table.add_column("Nickname", style="magenta")
    table.add_column("Level", style="green", justify="center")
    table.add_column("Region", style="yellow", justify="center")
    table.add_column("Category", style="bold white", justify="center")

    # ডাইনামিক কাউন্টার
    counts = {f: 0 for f in PANEL_FILTERS}
    failed_count = 0

    with Progress(
        SpinnerColumn(spinner_name="dots12"),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=30, complete_style="magenta"),
        TaskProgressColumn(),
        console=console
    ) as progress:
        task = progress.add_task("[bold cyan]Scanning accounts...[/bold cyan]", total=total_accounts)

        with ThreadPoolExecutor(max_workers=THREAD_LIMIT) as executor:
            futures = {executor.submit(process_account, acc): acc for acc in accounts_to_check}

            for future in as_completed(futures):
                result = future.result()
                if result:
                    if result["status"] == "success":
                        lvl = result["level"]
                        cat = result["category"]
                        
                        # ক্যাটাগরি থেকে ফিল্টারগুলো আলাদা করে কাউন্ট করা
                        for fk in cat.split(", "):
                            if fk in counts:
                                counts[fk] += 1
                                
                        cat_badge = f"[bold green]{cat}[/bold green]"
                        table.add_row(
                            result["account_id"],
                            result["nickname"],
                            str(result["level"]),
                            result["region"],
                            cat_badge
                        )
                    else:
                        failed_count += 1
                        table.add_row("N/A", "N/A", "N/A", "N/A", "[red]FAILED[/red]")
                
                progress.update(task, advance=1)

    console.print("\n")
    console.print(table)

    # সামারি ড্যাশবোর্ড
    summary_lines = ["[bold magenta]📊 CHECK SUMMARY REPORT 📊[/bold magenta]\n"]
    for fk, count in counts.items():
        if count > 0:
            summary_lines.append(f"🔹 {PANEL_FILTER_LABELS[fk]} : {count} -> {OUTPUT_FOLDER}/{fk}-lvl_Accounts.txt")
            
    summary_lines.append(f"\n[bold red]❌ Failed/Dead Accounts : {failed_count}[/bold red] -> {FAILED_FILE}")
    summary_lines.append(f"\n📌 Full Info Log : {SUCCESS_FILE}")
    summary_lines.append(f"📌 Details (Pass) Log : {DETAILS_FILE}")

    summary_text = "\n".join(summary_lines)
    console.print(Panel(summary_text, title="📊 REPORT", border_style="purple"))

# ═══════════════════════════════════════════════════════════════════
# PANEL SECTION (merged from app.py + panel_service.py)
# - main.py এর get_account_id() / get_account_info() reuse করে
# - Region-wise SEPARATE files: <region>_data.json
# - নতুন 4 filter: 22-30+, 20-21, 8-19, 3-8(BELOW)
# ═══════════════════════════════════════════════════════════════════

# ───────── Region file helpers ─────────
def _filename_for_region(region):
    return f"{region.strip().lower()}_data.json"


def _region_from_filename(fname):
    base = os.path.basename(fname)
    suffix = "_data.json"
    if base.lower().endswith(suffix):
        return base[:-len(suffix)].upper()
    return None


def _normalize_accounts(raw):
    """Accept list of dicts or legacy 'uid:pass' strings. Return list of {uid, password}."""
    result = []
    if isinstance(raw, dict):
        flat = []
        for v in raw.values():
            if isinstance(v, list):
                flat.extend(v)
        raw = flat
    if not isinstance(raw, list):
        return result

    for item in raw:
        if isinstance(item, dict):
            uid = str(item.get("uid", "")).strip()
            pwd = str(item.get("password", "")).strip()
            if uid:
                result.append({"uid": uid, "password": pwd})
        elif isinstance(item, str) and ":" in item:
            u, p = item.split(":", 1)
            u, p = u.strip(), p.strip()
            if u:
                result.append({"uid": u, "password": p})
    return result


def load_all_regions():
    """Scan directory for '*_data.json', return {REGION: [{"uid":..,"password":..}, ...]}"""
    data = {}
    for path in sorted(glob.glob("*_data.json")):
        region = _region_from_filename(path)
        if not region:
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            data[region] = []
            continue
        data[region] = _normalize_accounts(raw)
        # যদি ফাইলে কোনো ডাটা না থাকে, তবে সেটি UI-তে দেখাবে না
        if not data[region]: 
            del data[region] 
    return data


def save_region_data(region, accounts):
    if not isinstance(accounts, list):
        raise ValueError("accounts must be a list")
    cleaned = _normalize_accounts(accounts)
    region = region.strip().upper()
    if not region:
        raise ValueError("region is required")
    path = _filename_for_region(region)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, indent=2, ensure_ascii=False)
    return cleaned


def delete_region(region):
    path = _filename_for_region(region)
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


def _panel_safe_region(name):
    return "".join(c for c in name if c.isalnum() or c in "-_") or "UNKNOWN"


def get_regions_summary():
    data = load_all_regions()
    summary = {}
    for region, accounts in data.items():
        region_dir = os.path.join(PANEL_RESULTS_DIR, _panel_safe_region(region))
        counts = {}
        for f in PANEL_FILTERS:
            # .txt এর জায়গায় .json
            path = os.path.join(region_dir, f"{f}_Accounts.json")
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as fh:
                        counts[f] = len(json.load(fh))
                except:
                    counts[f] = 0
            else:
                counts[f] = 0
        summary[region] = {
            "input_count": len(accounts),
            "results": counts,
            "total_results": sum(counts.values()),
        }
    return summary


# ───────── Region-wise file saver ─────────
def _save_region_result(region, filter_key, uid_key, pwd_key):
    region_dir = os.path.join(PANEL_RESULTS_DIR, _panel_safe_region(region))
    os.makedirs(region_dir, exist_ok=True)
    path = os.path.join(region_dir, f"{filter_key}_Accounts.json")
    
    with PANEL_FILE_LOCK:
        data = []
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except:
                pass
        
        # ডুপ্লিকেট চেক
        for item in data:
            if str(item.get("uid")) == str(uid_key):
                return False
                
        # নতুন ডাটা যোগ করে JSON এ সেভ
        data.append({"uid": str(uid_key), "password": str(pwd_key)})
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True


def _save_region_failed(region, raw_line, reason):
    region_dir = os.path.join(PANEL_RESULTS_DIR, _panel_safe_region(region))
    os.makedirs(region_dir, exist_ok=True)
    path = os.path.join(region_dir, "Failed_Accounts.json")
    
    with PANEL_FILE_LOCK:
        data = []
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except:
                pass
        
        # UID এবং Password আলাদা করা
        uid = ""
        pwd = ""
        if ":" in raw_line:
            uid, pwd = raw_line.split(":", 1)
            uid, pwd = uid.strip(), pwd.strip()
        else:
            uid = raw_line.strip()
            
        # ডুপ্লিকেট চেক
        if not any(item.get("uid") == uid for item in data):
            data.append({
                "uid": uid, 
                "password": pwd, 
                "reason": reason
            })
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)


# ───────── Panel worker (reuses original API functions) ─────────
def _panel_process_account(region, account_obj):
    if isinstance(account_obj, dict):
        uid_in = str(account_obj.get("uid", "")).strip()
        pwd_in = str(account_obj.get("password", "")).strip()
    else:
        uid_in, _, pwd_in = str(account_obj).partition(":")
        uid_in, pwd_in = uid_in.strip(), pwd_in.strip()

    account_line = f"{uid_in}:{pwd_in}" if pwd_in else uid_in

    jwt = get_account_id(account_line)
    if not jwt.get("account_id"):
        _save_region_failed(region, account_line, jwt.get("error", "API1 fail"))
        return {"status": "failed", "reason": jwt.get("error", "API1 fail")}

    acc_id = str(jwt["account_id"])
    orig_uid = jwt.get("original_uid") or acc_id
    password = jwt.get("password") or pwd_in

    info = get_account_info(acc_id)
    if not info.get("data"):
        _save_region_failed(region, account_line, info.get("error", "API2 fail"))
        return {"status": "failed", "reason": info.get("error", "API2 fail")}

    raw = info["data"]
    if "BasicInformation" in raw:
        b = raw["BasicInformation"]
        name, lvl = b.get("Name", "Unknown"), b.get("Level", 0)
    elif "basicInfo" in raw:
        b = raw["basicInfo"]
        name, lvl = b.get("nickname", "Unknown"), b.get("level", 0)
    elif "AccountInfo" in raw:
        b = raw["AccountInfo"]
        name, lvl = b.get("AccountName", "Unknown"), b.get("AccountLevel", 0)
    else:
        name, lvl = "Unknown", 0

    try:
        lvl = int(lvl)
    except Exception:
        lvl = 0

    # === নতুন লজিক: সব মিলে যাওয়া ফিল্টারে সেভ করা ===
    matched_filters = [k for k, m in PANEL_FILTERS.items() if m(lvl)]
    if not matched_filters:
        matched_filters = ["3-8"] # ব্যাকআপ
        
    for fk in matched_filters:
        _save_region_result(region, fk, orig_uid, password)

    return {
        "status": "success",
        "account_id": acc_id,
        "nickname": name,
        "level": lvl,
        "region": region,
        "filters": matched_filters, # লিস্ট আকারে রিটার্ন হচ্ছে
        "credentials": f"{orig_uid}:{password}",
    }

def _panel_log(msg):
    PANEL_STATE["log"].append({"ts": datetime.now().strftime("%H:%M:%S"), "msg": msg})
    if len(PANEL_STATE["log"]) > 250:
        PANEL_STATE["log"] = PANEL_STATE["log"][-250:]


def start_panel_processing(region_choice="ALL"):
    if PANEL_STATE["running"]:
        return
    PANEL_STATE.update({
        "running": True, "done": False, "progress": 0,
        "counters": {f: 0 for f in PANEL_FILTERS},
        "failed": 0, "log": [], "per_region": {},
    })

    try:
        data = load_all_regions()
        regions = [region_choice] if region_choice != "ALL" else list(data.keys())

        tasks = []
        for reg in regions:
            for acc in data.get(reg, []):
                tasks.append((reg, acc))

        PANEL_STATE["total"] = len(tasks)
        _panel_log(f"🚀 Scan started • {len(tasks)} accounts across {len(regions)} region(s)")

        with ThreadPoolExecutor(max_workers=THREAD_LIMIT) as ex:
            futures = {ex.submit(_panel_process_account, r, a): (r, a) for r, a in tasks}
            for fut in as_completed(futures):
                r, a = futures[fut]
                PANEL_STATE["current_region"] = r
                PANEL_STATE["current_account"] = (
                    a.get("uid", "") if isinstance(a, dict) else str(a)
                )
                try:
                    res = fut.result()
                except Exception as e:
                    res = {"status": "failed", "reason": str(e)}

                pr = PANEL_STATE["per_region"].setdefault(
                    r, {**{f: 0 for f in PANEL_FILTERS}, "failed": 0}
                )
                if res["status"] == "success":
                    for fk in res.get("filters", []):
                        if fk in PANEL_STATE["counters"]:
                            PANEL_STATE["counters"][fk] += 1
                        if fk in pr:
                            pr[fk] += 1
                    _panel_log(f"✅ [{r}] {res['nickname']} • Lvl {res['level']} • {', '.join(res.get('filters', []))}")
                else:
                    PANEL_STATE["failed"] += 1
                    pr["failed"] += 1
                    _panel_log(f"❌ [{r}] FAILED • {res.get('reason', '')[:60]}")
                PANEL_STATE["progress"] += 1

        PANEL_STATE["done"] = True
        _panel_log("🏁 Scan complete")
    except Exception as e:
        _panel_log(f"💥 FATAL: {e}")
        PANEL_STATE["done"] = True
    finally:
        PANEL_STATE["running"] = False


def get_panel_status():
    return dict(PANEL_STATE)


def get_panel_results(region, filter_key):
    if filter_key not in PANEL_FILTERS:
        raise ValueError("Invalid filter")
    region_dir = os.path.join(PANEL_RESULTS_DIR, _panel_safe_region(region))
    path = os.path.join(region_dir, f"{filter_key}_Accounts.json") # .json
    
    if not os.path.exists(path):
        return []
    
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return []


# ═══════════════════════════════════════════════════════════════════
# FLASK APP + ROUTES
# ═══════════════════════════════════════════════════════════════════

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False


@app.route("/")
def _panel_index():
    return render_template("index.html")


@app.route("/api/data")
def _api_data():
    try:
        return jsonify({"ok": True, "regions": get_regions_summary()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/region/<region>")
def _api_region_get(region):
    try:
        data = load_all_regions()
        return jsonify({"ok": True, "accounts": data.get(region.upper(), [])})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/region/<region>", methods=["POST"])
def _api_region_save(region):
    try:
        payload = request.get_json(force=True) or {}
        accounts = payload.get("accounts", [])
        cleaned = save_region_data(region, accounts)
        return jsonify({"ok": True, "count": len(cleaned)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/region/<region>", methods=["DELETE"])
def _api_region_delete(region):
    try:
        existed = delete_region(region)
        return jsonify({"ok": True, "deleted": existed})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/start", methods=["POST"])
def _api_start():
    if PANEL_STATE["running"]:
        return jsonify({"ok": False, "error": "Already running"}), 409
    region = (request.get_json(silent=True) or {}).get("region", "ALL")
    threading.Thread(target=start_panel_processing, args=(region,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/status")
def _api_status():
    return jsonify(get_panel_status())


@app.route("/api/results/<region>/<filter_key>")
def _api_results(region, filter_key):
    try:
        return jsonify({"ok": True, "accounts": get_panel_results(region, filter_key)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/filters")
def _api_filters():
    return jsonify({"ok": True, "filters": list(PANEL_FILTERS.keys()),
                    "labels": PANEL_FILTER_LABELS})

@app.route("/api/clear", methods=["POST"])
def _api_clear():
    try:
        # Results ফোল্ডার ডিলিট
        if os.path.exists(PANEL_RESULTS_DIR):
            shutil.rmtree(PANEL_RESULTS_DIR)
            
        # সব bd_data.json, ind_data.json ফাইল ডিলিট
        for f in glob.glob("*_data.json"):
            try:
                os.remove(f)
            except:
                pass

        # PANEL_STATE রিসেট করা
        PANEL_STATE.update({
            "running": False, "done": True, "progress": 0, "total": 0,
            "current_region": None, "current_account": None,
            "counters": {f: 0 for f in PANEL_FILTERS},
            "failed": 0, "log": [], "per_region": {},
        })
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ═══════════════════════════════════════════════════════════════════
# PANEL LAUNCHER
# ═══════════════════════════════════════════════════════════════════

def run_panel():

    banner = (
        "\n[bold magenta]╔══════════════════════════════════════════════╗[/bold magenta]\n"
        "[bold magenta]║[/bold magenta]  [bold cyan]FF LEVEL FILTER — PANEL UI[/bold cyan]                  [bold magenta]║[/bold magenta]\n"
        "[bold magenta]║[/bold magenta]  [bold white]Region-wise • JSON • Live API[/bold white]               [bold magenta]║[/bold magenta]\n"
        f"[bold magenta]║[/bold magenta]  [bold green]→ http://localhost:{PORT}[/bold green]                     [bold magenta]║[/bold magenta]\n"
        "[bold magenta]╚══════════════════════════════════════════════╝[/bold magenta]\n"
        "[dim]Ctrl+C diye stop korun. CLI mode: [bold]python main.py --cli[/bold][/dim]\n"
    )
    console.print(banner)

    try:
        app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        console.print("\n[bold red][!] Panel stopped.[/bold red]")

if __name__ == "__main__":
    if "--cli" in sys.argv:
        # পুরনো CLI mode — main() untouched
        try:
            main()
        except KeyboardInterrupt:
            console.print(f"\n[bold red][!] Program stop kora hoyeche![/bold red]")
            sys.exit()
    else:
        # Default: Panel UI
        run_panel()