using System.Diagnostics;
using System.Runtime.InteropServices;

namespace SaltPlayer.Native.Services;

/// <summary>
/// Keeps the Python backend and every ffmpeg it spawns inside a Win32 job object with
/// JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE. If LuHuiMusic.exe crashes or is killed from Task
/// Manager, the kernel closes the job handle and takes the whole audio tree with it —
/// otherwise an orphan holding a WASAPI exclusive lock can leave the machine silent.
/// </summary>
internal static class JobObjectHelper
{
    private const int JobObjectExtendedLimitInformationClass = 9;
    private const uint JobObjectLimitKillOnJobClose = 0x2000;

    [StructLayout(LayoutKind.Sequential)]
    private struct JobObjectBasicLimitInformation
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IoCounters
    {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JobObjectExtendedLimitInformation
    {
        public JobObjectBasicLimitInformation BasicLimitInformation;
        public IoCounters IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateJobObject(IntPtr jobAttributes, string? name);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(
        IntPtr job, int infoClass, IntPtr info, uint infoLength);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);

    /// <summary>Returns a job handle on success, or IntPtr.Zero when the OS refuses.</summary>
    public static IntPtr Attach(Process process)
    {
        var job = IntPtr.Zero;
        var info = IntPtr.Zero;
        try
        {
            job = CreateJobObject(IntPtr.Zero, null);
            if (job == IntPtr.Zero) return IntPtr.Zero;

            var extended = new JobObjectExtendedLimitInformation();
            extended.BasicLimitInformation.LimitFlags = JobObjectLimitKillOnJobClose;
            var length = Marshal.SizeOf<JobObjectExtendedLimitInformation>();
            info = Marshal.AllocHGlobal(length);
            Marshal.StructureToPtr(extended, info, false);
            if (!SetInformationJobObject(job, JobObjectExtendedLimitInformationClass, info, (uint)length))
            {
                CloseHandle(job);
                return IntPtr.Zero;
            }
            if (!AssignProcessToJobObject(job, process.Handle))
            {
                CloseHandle(job);
                return IntPtr.Zero;
            }
            return job;
        }
        catch
        {
            if (job != IntPtr.Zero) CloseHandle(job);
            return IntPtr.Zero;
        }
        finally
        {
            if (info != IntPtr.Zero) Marshal.FreeHGlobal(info);
        }
    }

    public static void Close(IntPtr job)
    {
        if (job != IntPtr.Zero)
        {
            try { CloseHandle(job); } catch { }
        }
    }
}
