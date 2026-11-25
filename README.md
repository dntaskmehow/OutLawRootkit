**By [dontaskmehow](https://github.com/dntaskmehow)** • **Project:** OutLaw Rootkit v2.1.6

> **Disclaimer:** *“All of my work is for ethical and research purposes. All Code if used is to be used on authorized targets. I’m not responsible for what user’s do with this. Also all code is my PoC and Critique is welcomed. Thank you all for your support.”

 > The exploits in this post is licensed under the MIT License (Copyright © 2026 dontaskmehow). The written article text is licensed under CC BY 4.0.

---

## OutLaw Rootkit v2.1.6

Modern cross-platform rootkits increasingly combine kernel-level privilege escalation with firmware persistence to evade traditional endpoint detection.

By chaining operating-system-specific vulnerabilities—such as `io_uring` use-after-free flaws in Linux Kernel 6.8 and PrintSpooler exploits in Windows 11 24H2—with UEFI bootkits, ChaCha20-Poly1305 encrypted command-and-control (C2) channels, and automated cloud credential harvesting, multi-stage frameworks like **OUTLAW-ROOTKIT v2.1.6** operate completely below standard userspace visibility.

In this post, I break down how I engineered `outlaw_rootkit.py` as a single-file, dual-OS architecture, walk through the design trade-offs behind its hooking and persistence pipeline, and examine what it takes to detect deep intrusion when standard userspace tools can no longer be trusted.

---

## How I Built It: Design Decisions & Engineering Trade-Offs

When building **OUTLAW-ROOTKIT v2.1.6** in an isolated lab environment, my goal was to see how a single, self-contained codebase (`outlaw_rootkit.py`) could execute a complete multi-stage kill chain across both **Linux Kernel 6.8** and **Windows 11 24H2**. Every architectural choice was driven by reducing disk footprint, avoiding static signatures, and maintaining cross-platform parity:

### 1. Why a Single-File Cross-Platform Core (`outlaw_rootkit.py`)?

* **What I did:** Instead of splitting the project into dozens of helper scripts, I consolidated all seven stages—from privilege escalation to C2 exfiltration—into `outlaw_rootkit.py`, using runtime OS detection to route execution between Linux and Windows subroutines.
* **Why:** Dropping multiple files onto a target filesystem increases the forensic footprint and triggers file-creation alerts. A single Python orchestrator keeps the deployment clean while sharing one unified `C2Config` engine, credential harvester, and subnet scanner across both operating systems.

### 2. Why Function-Level Hooking Over Simple Process Renaming?

* **What I did:** On Linux, I used `LD_PRELOAD` to intercept `getdents64`, `kill`, and `tcp4_seq_show`. On Windows, I targeted `CreateProcessA`, `FindFirstFileA`, and `GetTickCount` via API hooking.
* **Why:** Renaming a binary doesn't fool `ps`, `netstat`, `ss`, or Task Manager. By intercepting the underlying directory-enumeration, socket-listing, and process-creation calls directly, the rootkit scrubs its own files, PIDs, and C2 ports from standard userspace inspection tools—and skews `GetTickCount` to disrupt sandbox timing checks.

### 3. Why Tiered Persistence (Userspace to UEFI)?

* **What I did:** I built five distinct persistence mechanisms: Crontab and `initramfs` alongside EFI variable manipulation on Linux, and Windows Services paired with PowerShell profile hooks on Windows.
* **Why:** Incident responders typically check scheduled tasks and active services first. Layering persistence from login profiles (`PowerShell`) and cron jobs down to early-boot `initramfs` and UEFI ensures that surviving a reboot or partial cleanup doesn't rely on a single foothold.

### 4. Why ChaCha20-Poly1305, Jittered Beacons, and Polymorphic Stubs?

* **What I did:** I configured the `C2Config` dataclass with a 30-second beacon interval, a `0.15` randomized jitter factor, and **ChaCha20-Poly1305** encryption backed by a local master key (`/tmp/macc_master_key.pem`). On top of that, I added a polymorphic stub generator that wraps each infection with randomized arithmetic operations and a unique decryptor stub.
* **Why:** Static YARA rules and SHA-256 blocklists rely on identical binary hashes, while network sensors flag rigid, predictable callback intervals and standard ransomware ciphers. Randomizing both the loader stub and the beacon timing eliminates static file signatures and breaks up periodic network telemetry.

---

**Researcher:** [dontaskmehow](https://github.com/dntaskmehow) (`https://github.com/dntaskmehow`)

---

## What This Is

A polymorphic, multi-stage rootkit targeting **Linux Kernel 6.8** and **Windows 11 24H2** that performs:

1. **Privilege Escalation** — Dirty Pipe v3 + `io_uring` UAF (Linux) / PrintSpooler + `CoCreateInstance` (Windows)
2. **Rootkit Installation** — Hides files, processes, and ports from all userspace tools
3. **Persistence** — Crontab, `initramfs`, UEFI bootkit, Windows services, PowerShell profile hooks
4. **C2 Backdoor** — Encrypted reverse shell with full command execution
5. **Credential Harvesting** — AWS, GCP, Azure, SSH, git, and local credential files
6. **Network Scanning** — Local network discovery and SSH pivot to compromised hosts
7. **Data Exfiltration** — Encrypted file and credential exfil to C2 server

---

## Zero-Day Exploit Chain

| Stage | Technique | Target |
| :--- | :--- | :--- |
| 1 | `io_uring` fixed-file UAF | Linux Kernel 6.8 |
| 2 | Pipe page-cache aliasing | `/etc/passwd` overwrite |
| 3 | BPF token TOCTOU race | `CAP_SYS_ADMIN` grant |
| 4 | `LD_PRELOAD` hooking | Process/file hiding |
| 5 | API hook injection | Windows process hiding |

## Persistence Vectors

| Vector | Platform | Mechanism |
| :--- | :--- | :--- |
| Crontab | Linux | Auto-reload rootkit every minute |
| Initramfs | Linux | Module loaded at boot before init |
| UEFI Bootkit | Linux | EFI variable manipulation |
| Windows Service | Windows | Auto-start system service |
| PowerShell Profile | Windows | Auto-start C2 beacon on login |

## Anti-Forensics

- `LD_PRELOAD` hooks on `getdents64`, `kill`, `tcp4_seq_show`
- API hooking on `CreateProcessA`, `FindFirstFileA`, `GetTickCount`
- Randomized timing jitter on C2 beacons
- Encrypted exfiltration via ChaCha20-Poly1305

## Files

| File | Description |
| :--- | :--- |
| `outlaw_rootkit.py` | Main rootkit script (all stages) |
| `requirements.txt` | Python dependencies |
| `README.md` | This file |

## Notes

- **Polymorphic**: Each infection generates a unique decryptor stub — no two binaries look the same.
- **Cross-platform**: Works on Linux 6.8 and Windows 11 24H2.
- **No AV signatures**: Uses ChaCha20-Poly1305 (non-standard for ransomware) and randomized arithmetic operations.

---

## Lab Setup & Installation

Run in an isolated, host-only virtual lab environment:

```bash
# Install dependencies
pip install -r requirements.txt

# Run the rootkit (requires elevated privileges in lab VM)
python3 outlaw_rootkit.py
```

## C2 Configuration

Edit the `C2Config` dataclass in `outlaw_rootkit.py`:

```python
@dataclass
class C2Config:
    host: str = "192.168.1.100"   # C2 server IP
    port: int = 4444              # C2 server port
    beacon_interval: int = 30     # Seconds between beacon attempts
    jitter: float = 0.15          # Random jitter for C2 traffic
    payload_b64: str = ""         # Optional base64 payload
    master_key_path: str = "/tmp/macc_master_key.pem"
```

### C2 Command Reference & Beacon Flow

| Command | Description |
| :--- | :--- |
| `EXEC:<cmd>` | Execute shell command |
| `INFO` | Get system info (hostname, kernel, PID, user) |
| `SCAN` | Scan for open ports, processes, credential files |
| `READ:<path>` | Read file contents |
| `WRITE:<path>:<content>` | Write file |
| `EXFIL:<path>` | Exfiltrate encrypted file |
| `EXIT` | Disconnect from C2 |
---

## Defensive Countermeasures & Detection Matrix

Because `LD_PRELOAD` and Win32 API hooks blind standard userspace utilities (`ps`, `ss`, `netstat`, Task Manager), defenders have to shift telemetry down to the kernel, hypervisor, and firmware layers:

| Rootkit Layer (`v2.1.6`) | Blinded Userspace Tool | High-Signal Defensive Telemetry |
| :--- | :--- | :--- |
Use code with caution.
| io_uring UAF & BPF Race | Standard syslog / auditd | eBPF LSM hooks, disabling unprivileged io_uring (kernel.io_uring_disabled=2) |
| LD_PRELOAD (getdents64, tcp4_seq_show) | ls, ps, ss, netstat | Direct /proc vs. getdents64 cross-view diffing, /etc/ld.so.preload FIM alerts |
| Win32 API Hooks (CreateProcessA, GetTickCount) | Task Manager, user-mode EDR | ETW-Ti (Threat Intelligence) kernel callbacks, PPL (Protected Process Light) enforcement |
| UEFI Bootkit & initramfs | OS-level AV scans | Hardware-rooted Secure Boot, TPM 2.0 Measured Boot attestation, EFI variable auditing |
| ChaCha20 C2 (0.15 Jitter) & Cloud Exfil | Static IP/hash blocklists | JA4+ network fingerprinting, IAM anomaly alerts on boto3/GCP/Azure SDK token reuse |

---
> **Disclaimer:** *“All of my work is for ethical and research purposes. All Code if used is to be used on authorized targets. I’m not responsible for what user’s do with this. Also all code is my PoC and Critique is welcomed. Thank you all for your support.”

 > The exploits in this post is licensed under the MIT License (Copyright © 2026 dontaskmehow). The written article text is licensed under CC BY 4.0.
