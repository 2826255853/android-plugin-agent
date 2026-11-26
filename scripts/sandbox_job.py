"""Own the Windows processes and resource limits for one sandbox run."""

import ctypes
from ctypes import wintypes as win


class BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
        ("flags", win.DWORD), ("min_working_set", ctypes.c_size_t),
        ("max_working_set", ctypes.c_size_t), ("active_processes", win.DWORD),
        ("affinity", ctypes.c_size_t), ("priority", win.DWORD), ("scheduling", win.DWORD),
    ]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", BasicLimits), ("io", ctypes.c_uint64 * 6),
        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t),
    ]


class CpuLimit(ctypes.Structure):
    _fields_ = [("flags", win.DWORD), ("rate", win.DWORD)]


class Accounting(ctypes.Structure):
    _fields_ = [("times", ctypes.c_int64 * 4), ("faults", win.DWORD),
                ("total", win.DWORD), ("active", win.DWORD), ("terminated", win.DWORD)]


class WindowsJob:
    def __enter__(self):
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, result, args in (
            ("CreateJobObjectW", win.HANDLE, (ctypes.c_void_p, win.LPCWSTR)),
            ("OpenProcess", win.HANDLE, (win.DWORD, win.BOOL, win.DWORD)),
            ("SetInformationJobObject", win.BOOL, (win.HANDLE, ctypes.c_int, ctypes.c_void_p, win.DWORD)),
            ("QueryInformationJobObject", win.BOOL,
             (win.HANDLE, ctypes.c_int, ctypes.c_void_p, win.DWORD, ctypes.c_void_p)),
            ("AssignProcessToJobObject", win.BOOL, (win.HANDLE, win.HANDLE)),
            ("TerminateJobObject", win.BOOL, (win.HANDLE, win.UINT)),
            ("CloseHandle", win.BOOL, (win.HANDLE,)),
        ):
            function = getattr(self.api, name)
            function.restype = result
            function.argtypes = args
        self.handle = self.checked(self.api.CreateJobObjectW(None, None))
        try:
            limits = ExtendedLimits()
            # ACTIVE_PROCESS | JOB_MEMORY | KILL_ON_JOB_CLOSE
            limits.basic.flags = 0x0008 | 0x0200 | 0x2000
            limits.basic.active_processes = 2  # Trusted launcher and VM only.
            limits.job_memory = 3_072 * 1024**2
            self.configure(9, limits)  # JobObjectExtendedLimitInformation
            self.configure(15, CpuLimit(0x01 | 0x04, 2500))  # ENABLE | HARD_CAP, 25%
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *_exc):
        self.close()

    def close(self):
        if self.handle is not None:
            self.checked(self.api.CloseHandle(self.handle))
            self.handle = None

    @staticmethod
    def checked(result):
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())
        return result

    def configure(self, kind, value):
        self.checked(self.api.SetInformationJobObject(self.handle, kind, ctypes.byref(value), ctypes.sizeof(value)))

    def assign(self, pid):
        process = self.checked(self.api.OpenProcess(0x0100 | 0x0001, False, pid))  # SET_QUOTA | TERMINATE
        try:
            self.checked(self.api.AssignProcessToJobObject(self.handle, process))
        finally:
            self.checked(self.api.CloseHandle(process))

    def stop(self):
        self.checked(self.api.TerminateJobObject(self.handle, 1))

    def counts(self):
        value = Accounting()
        self.checked(self.api.QueryInformationJobObject(self.handle, 1, ctypes.byref(value),
                                                       ctypes.sizeof(value), None))
        return value.total, value.active
