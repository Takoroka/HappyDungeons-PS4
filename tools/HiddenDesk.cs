// Starts a program on a separate, never-shown Windows desktop and captures its windows.
// Used for Happy Dungeons (PS4) test runs so the game never appears on the user's screen.
using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

public static class HiddenDesk
{
    const uint DESKTOP_ALL = 0x01FF;          // all desktop-specific rights
    const uint GENERIC_ALL = 0x10000000;
    const uint CREATE_NO_WINDOW = 0x08000000;
    const uint STARTF_USESTDHANDLES = 0x100;
    const uint STARTF_USESHOWWINDOW = 0x1;

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct STARTUPINFO
    {
        public int cb; public string lpReserved; public string lpDesktop; public string lpTitle;
        public int dwX, dwY, dwXSize, dwYSize, dwXCountChars, dwYCountChars, dwFillAttribute;
        public uint dwFlags; public short wShowWindow, cbReserved2; public IntPtr lpReserved2;
        public IntPtr hStdInput, hStdOutput, hStdError;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct PROCESS_INFORMATION { public IntPtr hProcess, hThread; public int dwProcessId, dwThreadId; }
    [StructLayout(LayoutKind.Sequential)]
    struct RECT { public int Left, Top, Right, Bottom; }

    [DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern IntPtr CreateDesktop(string name, IntPtr dev, IntPtr devmode, uint flags, uint access, IntPtr sa);
    [DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern IntPtr OpenDesktop(string name, uint flags, bool inherit, uint access);
    [DllImport("user32.dll", SetLastError = true)] static extern bool SetThreadDesktop(IntPtr h);
    [DllImport("user32.dll")] static extern bool CloseDesktop(IntPtr h);
    delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] static extern bool EnumDesktopWindows(IntPtr hDesk, EnumWindowsProc fn, IntPtr lParam);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hWnd, out int pid);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetWindowText(IntPtr hWnd, StringBuilder sb, int max);
    [DllImport("user32.dll")] static extern bool GetClientRect(IntPtr hWnd, out RECT r);
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr hWnd, out RECT r);
    [DllImport("user32.dll")] static extern bool PrintWindow(IntPtr hWnd, IntPtr hdc, uint flags);
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern bool CreateProcess(string app, StringBuilder cmd, IntPtr pa, IntPtr ta, bool inherit, uint flags,
        IntPtr env, string cwd, ref STARTUPINFO si, out PROCESS_INFORMATION pi);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);

    // Creates (or opens) the hidden desktop and starts exe there. Returns the process id.
    public static int Launch(string desktop, string exe, string args, string cwd)
    {
        IntPtr d = CreateDesktop(desktop, IntPtr.Zero, IntPtr.Zero, 0, DESKTOP_ALL | GENERIC_ALL, IntPtr.Zero);
        if (d == IntPtr.Zero) throw new Exception("CreateDesktop failed: " + Marshal.GetLastWin32Error());
        STARTUPINFO si = new STARTUPINFO();
        si.cb = Marshal.SizeOf(typeof(STARTUPINFO));
        si.lpDesktop = "WinSta0\\" + desktop;
        si.dwFlags = STARTF_USESHOWWINDOW; si.wShowWindow = 4; // SW_SHOWNOACTIVATE
        PROCESS_INFORMATION pi;
        StringBuilder cmd = new StringBuilder("\"" + exe + "\" " + args);
        if (!CreateProcess(null, cmd, IntPtr.Zero, IntPtr.Zero, false, CREATE_NO_WINDOW, IntPtr.Zero, cwd, ref si, out pi))
            throw new Exception("CreateProcess failed: " + Marshal.GetLastWin32Error());
        CloseHandle(pi.hThread); CloseHandle(pi.hProcess);
        // The desktop handle is intentionally left open until this host process exits;
        // the started process keeps the desktop alive afterwards.
        return pi.dwProcessId;
    }

    [DllImport("user32.dll")] static extern bool PostMessage(IntPtr hWnd, uint msg, IntPtr w, IntPtr l);
    [DllImport("kernel32.dll")] static extern void Sleep(uint ms);

    // Posts a key press (down, hold, up) to the main window of pid on the hidden desktop.
    public static string SendKey(string desktop, int pid, int vk, int scan, bool extended, int holdMs)
    {
        string result = null;
        Thread t = new Thread(() => {
            IntPtr d = OpenDesktop(desktop, 0, false, DESKTOP_ALL | GENERIC_ALL);
            if (d == IntPtr.Zero) { result = "OpenDesktop failed"; return; }
            SetThreadDesktop(d);
            IntPtr best = FindMainWindow(d, pid);
            if (best == IntPtr.Zero) { result = "no window"; CloseDesktop(d); return; }
            long l = 1 | ((long)scan << 16) | (extended ? (1L << 24) : 0);
            PostMessage(best, 0x0100, (IntPtr)vk, (IntPtr)l);                         // WM_KEYDOWN
            Sleep((uint)Math.Max(1, holdMs));
            PostMessage(best, 0x0101, (IntPtr)vk, (IntPtr)(l | (1L << 30) | (1L << 31))); // WM_KEYUP
            result = "posted vk=0x" + vk.ToString("X") + " to " + best;
            CloseDesktop(d);
        });
        t.Start(); t.Join();
        return result;
    }

    static IntPtr FindMainWindow(IntPtr d, int pid)
    {
        IntPtr best = IntPtr.Zero; int bestArea = 0;
        EnumDesktopWindows(d, (h, l) => {
            int p; GetWindowThreadProcessId(h, out p);
            if (p == pid && IsWindowVisible(h))
            {
                RECT r; GetWindowRect(h, out r);
                int area = (r.Right - r.Left) * (r.Bottom - r.Top);
                if (area > bestArea) { best = h; bestArea = area; }
            }
            return true;
        }, IntPtr.Zero);
        return best;
    }

    // Lists the windows of pid on the hidden desktop and saves a PrintWindow capture of the largest one.
    public static string Capture(string desktop, int pid, string pngPath)
    {
        string result = null;
        Thread t = new Thread(() => { result = CaptureOnThread(desktop, pid, pngPath); });
        t.Start(); t.Join();
        return result;
    }

    static string CaptureOnThread(string desktop, int pid, string pngPath)
    {
        IntPtr d = OpenDesktop(desktop, 0, false, DESKTOP_ALL | GENERIC_ALL);
        if (d == IntPtr.Zero) return "OpenDesktop failed: " + Marshal.GetLastWin32Error();
        if (!SetThreadDesktop(d)) { int e = Marshal.GetLastWin32Error(); CloseDesktop(d); return "SetThreadDesktop failed: " + e; }
        List<IntPtr> wins = new List<IntPtr>();
        StringBuilder log = new StringBuilder();
        EnumDesktopWindows(d, (h, l) => {
            int p; GetWindowThreadProcessId(h, out p);
            if (p == pid) wins.Add(h);
            return true;
        }, IntPtr.Zero);
        IntPtr best = IntPtr.Zero; int bestArea = 0;
        foreach (IntPtr h in wins)
        {
            RECT r; GetWindowRect(h, out r);
            StringBuilder sb = new StringBuilder(256); GetWindowText(h, sb, 256);
            int area = (r.Right - r.Left) * (r.Bottom - r.Top);
            log.AppendFormat("win {0} vis={1} rect=({2},{3},{4},{5}) '{6}'\n", h, IsWindowVisible(h), r.Left, r.Top, r.Right, r.Bottom, sb);
            if (IsWindowVisible(h) && area > bestArea) { best = h; bestArea = area; }
        }
        if (best != IntPtr.Zero && pngPath != null && pngPath.Length > 0)
        {
            RECT c; GetClientRect(best, out c);
            int w = Math.Max(1, c.Right - c.Left), hgt = Math.Max(1, c.Bottom - c.Top);
            using (Bitmap bmp = new Bitmap(w, hgt, PixelFormat.Format32bppArgb))
            {
                using (Graphics g = Graphics.FromImage(bmp))
                {
                    IntPtr hdc = g.GetHdc();
                    bool ok = PrintWindow(best, hdc, 3); // PW_CLIENTONLY | PW_RENDERFULLCONTENT
                    g.ReleaseHdc(hdc);
                    log.AppendFormat("PrintWindow={0} size={1}x{2}\n", ok, w, hgt);
                }
                bmp.Save(pngPath, ImageFormat.Png);
            }
        }
        CloseDesktop(d);
        return log.ToString();
    }
}
