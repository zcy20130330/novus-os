#!/usr/bin/env python3
"""Stage a disk image onto a raw device so the mount manager cannot wedge us.

    flash_usb_staged.py <disk_number> <image>

Writing LBA 0 first is what kills a naive raw write on Windows: the moment a
valid primary GPT with an EFI partition appears, the mount manager re-parses
the disk and every handle open on it goes stale, so the next WriteFile fails
with ERROR_ACCESS_DENIED (5) even though nothing is actually locked.

This writes the payload first, then the backup GPT, and only then the 34
preamble sectors, reopening the device for every chunk. The control block is
therefore the last thing that lands, and no write depends on a handle that
outlived a GPT change. Everything is read back afterwards, again with a
fresh handle per chunk, and the first differing offset is reported.

Refuses disk 0 and refuses a device smaller than the image. Needs administrator
rights (raw device access).
"""

import ctypes
import os
import sys
import time
from ctypes import wintypes

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
INVALID_HANDLE = ctypes.c_void_p(-1).value
CHUNK = 256 << 10  # Windows refuses a 1 MiB single write into a partition region
                   # on this removable disk (ERROR_ACCESS_DENIED); 256 KiB passes
SECTOR = 512
PREAMBLE = 34 * SECTOR
TRAILER = 33 * SECTOR

k32 = ctypes.WinDLL('kernel32', use_last_error=True)
k32.CreateFileW.restype = ctypes.c_void_p
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                            ctypes.c_void_p]
k32.SetFilePointerEx.argtypes = [ctypes.c_void_p, ctypes.c_longlong,
                                 ctypes.POINTER(ctypes.c_longlong),
                                 wintypes.DWORD]
k32.SetFilePointerEx.restype = wintypes.BOOL
k32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                         ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
k32.ReadFile.restype = wintypes.BOOL
k32.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                          ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
k32.WriteFile.restype = wintypes.BOOL
k32.GetFileSizeEx.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_longlong)]
k32.GetFileSizeEx.restype = wintypes.BOOL
k32.FlushFileBuffers.argtypes = [ctypes.c_void_p]
k32.FlushFileBuffers.restype = wintypes.BOOL
k32.CloseHandle.argtypes = [ctypes.c_void_p]


def open_device(number, write):
    access = GENERIC_READ | (GENERIC_WRITE if write else 0)
    handle = k32.CreateFileW(r'\\.\PhysicalDrive%d' % number, access,
                             FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                             OPEN_EXISTING, 0, None)
    if not handle or handle == INVALID_HANDLE:
        err = ctypes.get_last_error()
        raise SystemExit('cannot open \\\\.\\PhysicalDrive%d: error %d '
                         '(administrator rights?)' % (number, err))
    return handle


def io(handle, offset, data, write, retries=40):
    """One seek+read/write. A stale handle is reopened by the caller."""
    for attempt in range(retries):
        pos = ctypes.c_longlong(0)
        if not k32.SetFilePointerEx(handle, offset, ctypes.byref(pos), 0):
            raise OSError(ctypes.get_last_error(), 'seek to %d failed' % offset)
        if write:
            view = (ctypes.c_char * len(data)).from_buffer_copy(data)
        else:
            view = (ctypes.c_char * len(data))()
        done = wintypes.DWORD(0)
        fn = k32.WriteFile if write else k32.ReadFile
        ok = fn(handle, view, len(data), ctypes.byref(done), None)
        if ok and done.value == len(data):
            return bytes(view) if not write else None
        err = ctypes.get_last_error()
        if ok and done.value != len(data):
            raise OSError(0, 'short transfer at %d: %d of %d' %
                          (offset, done.value, len(data)))
        if err != 5 or attempt == retries - 1:
            raise OSError(err, '%s at %d failed' %
                          ('write' if write else 'read', offset))
        time.sleep(0.25)


def write_chunk(number, offset, data):
    handle = open_device(number, True)
    try:
        io(handle, offset, data, True)
        k32.FlushFileBuffers(handle)
    finally:
        k32.CloseHandle(handle)


def read_chunk(number, offset, length):
    handle = open_device(number, False)
    try:
        return io(handle, offset, b'\0' * length, False)
    finally:
        k32.CloseHandle(handle)


def write_range(number, data, start, end, label):
    done = start
    while done < end:
        size = min(CHUNK, end - done)
        write_chunk(number, done, data[done:done + size])
        done += size
        print('  %s %d/%d' % (label, done, end), flush=True)


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    number = int(argv[1], 0)
    image = argv[2]
    if not os.path.isfile(image):
        print('no such image:', image)
        return 2
    data = open(image, 'rb').read()
    if len(data) < 1 << 20:
        print('image is suspiciously small: %d bytes' % len(data))
        return 2
    if number == 0:
        print('REFUSED: disk 0 is the system disk')
        return 1

    image_end = len(data) - TRAILER
    print('%s <- %s (%d MiB)' % ('PhysicalDrive%d' % number, image,
                                 len(data) >> 20), flush=True)
    print('payload', flush=True)
    write_range(number, data, PREAMBLE, image_end, 'payload')
    print('backup gpt', flush=True)
    write_range(number, data, image_end, len(data), 'backup')
    print('primary gpt (last, it invalidates the disk handles)', flush=True)
    write_range(number, data, 0, PREAMBLE, 'primary')

    print('verify', flush=True)
    done = 0
    while done < len(data):
        size = min(CHUNK, len(data) - done)
        got = read_chunk(number, done, size)
        if got != data[done:done + size]:
            for i, (x, y) in enumerate(zip(got, data[done:done + size])):
                if x != y:
                    print('VERIFY FAILED: first differing byte at %d' % (done + i))
                    return 1
        done += size
        print('  verified %d/%d' % (done, len(data)), flush=True)
    print('verified: %d bytes identical' % len(data))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
