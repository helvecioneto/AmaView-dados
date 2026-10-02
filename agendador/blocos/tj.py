"""
TurboJPEG (API 2.x) por ctypes, para o meio_km.py: decodificar o JPEG do STAR
em cinza e codificar os blocos de 0,5 km (1 canal, progressivo).
"""
import ctypes
import ctypes.util
import os

import numpy as np

TJPF_GRAY = 6
TJSAMP_GRAY = 3
TJFLAG_PROGRESSIVE = 16384
TJFLAG_ACCURATEDCT = 4096

_tj = None


def _lib():
    global _tj
    if _tj is None:
        caminho = os.environ.get("BLOCOS_LIBTURBOJPEG") or ctypes.util.find_library("turbojpeg") or "libturbojpeg.so.0"
        lib = ctypes.CDLL(caminho)
        lib.tjInitCompress.restype = ctypes.c_void_p
        lib.tjInitDecompress.restype = ctypes.c_void_p
        lib.tjCompress2.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)), ctypes.POINTER(ctypes.c_ulong),
            ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ]
        lib.tjDecompressHeader3.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
        ]
        lib.tjDecompress2.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ]
        lib.tjFree.argtypes = [ctypes.POINTER(ctypes.c_ubyte)]
        lib.tjDestroy.argtypes = [ctypes.c_void_p]
        lib.tjGetErrorStr2.argtypes = [ctypes.c_void_p]
        lib.tjGetErrorStr2.restype = ctypes.c_char_p
        _tj = lib
    return _tj


def decodificar_cinza(jpeg: bytes) -> np.ndarray:
    """JPEG → matriz uint8 (linhas, colunas), em cinza (a luminância, se for colorido)."""
    lib = _lib()
    h = lib.tjInitDecompress()
    try:
        w, a, sub, cor = ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        if lib.tjDecompressHeader3(h, jpeg, len(jpeg), ctypes.byref(w), ctypes.byref(a), ctypes.byref(sub), ctypes.byref(cor)):
            raise ValueError(lib.tjGetErrorStr2(h).decode())
        saida = np.empty((a.value, w.value), np.uint8)
        if lib.tjDecompress2(h, jpeg, len(jpeg), saida.ctypes.data, w.value, w.value, a.value, TJPF_GRAY, TJFLAG_ACCURATEDCT):
            erro = lib.tjGetErrorStr2(h).decode()
            # Aviso (ex.: dados extras no fim) não impede a imagem; arquivo truncado sim.
            if "Premature end" in erro:
                raise ValueError(erro)
        return saida
    finally:
        lib.tjDestroy(h)


def codificar_cinza(img: np.ndarray, qualidade: int = 92, progressivo: bool = True) -> bytes:
    """Matriz uint8 (linhas, colunas) → JPEG de 1 canal."""
    lib = _lib()
    img = np.ascontiguousarray(img, dtype=np.uint8)
    a, w = img.shape
    h = lib.tjInitCompress()
    buf = ctypes.POINTER(ctypes.c_ubyte)()
    tam = ctypes.c_ulong(0)
    try:
        flags = (TJFLAG_PROGRESSIVE if progressivo else 0) | TJFLAG_ACCURATEDCT
        if lib.tjCompress2(h, img.ctypes.data, w, w, a, TJPF_GRAY, ctypes.byref(buf), ctypes.byref(tam), TJSAMP_GRAY, qualidade, flags):
            raise ValueError(lib.tjGetErrorStr2(h).decode())
        return ctypes.string_at(buf, tam.value)
    finally:
        if buf:
            lib.tjFree(buf)
        lib.tjDestroy(h)
