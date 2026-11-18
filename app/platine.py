"""Ouvre la connexion de sortie des platines DV qui n'émettent qu'à la demande."""
import ctypes, struct

_raw = ctypes.CDLL("libraw1394.so.11")
_iec = ctypes.CDLL("libiec61883.so.0")
_raw.raw1394_new_handle.restype = ctypes.c_void_p
_raw.raw1394_destroy_handle.argtypes = [ctypes.c_void_p]
_raw.raw1394_get_port_info.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
_raw.raw1394_set_port.argtypes = [ctypes.c_void_p, ctypes.c_int]
_raw.raw1394_get_nodecount.argtypes = [ctypes.c_void_p]
_raw.raw1394_get_local_id.argtypes = [ctypes.c_void_p]
_raw.raw1394_get_local_id.restype = ctypes.c_uint16
_raw.raw1394_read.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint64, ctypes.c_size_t, ctypes.c_void_p]
_iec.iec61883_cmp_connect.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.POINTER(ctypes.c_int),
                                      ctypes.c_uint16, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
OPCR0 = 0xFFFFF0000904  # premier registre de sortie (oPCR[0]) d'un appareil AV/C


def ouvrir_sorties(connecter=True):
    """Parcourt le bus ; pour chaque sortie en ligne sans aucune connexion, en ouvre une.

    Rend la liste (port, nœud, en_ligne, diffusion, connexions, canal, action).
    """
    bilan = []
    poignee = _raw.raw1394_new_handle()
    if not poignee:
        return bilan
    try:
        for port in range(_raw.raw1394_get_port_info(poignee, None, 0)):
            if _raw.raw1394_set_port(poignee, port) < 0:
                continue
            local = _raw.raw1394_get_local_id(poignee)
            for n in range(_raw.raw1394_get_nodecount(poignee)):
                noeud = 0xFFC0 | n
                if noeud == local:
                    continue
                tampon = ctypes.create_string_buffer(4)
                if _raw.raw1394_read(poignee, noeud, OPCR0, 4, tampon) < 0:
                    continue
                q = struct.unpack(">I", tampon.raw)[0]
                en_ligne, diffusion, connexions, canal = q >> 31, (q >> 30) & 1, (q >> 24) & 0x3F, (q >> 16) & 0x3F
                action = ""
                if connecter and en_ligne and not diffusion and not connexions:
                    sortie, entree, bande = ctypes.c_int(-1), ctypes.c_int(-1), ctypes.c_int(0)
                    canal = _iec.iec61883_cmp_connect(poignee, noeud, ctypes.byref(sortie), local,
                                                      ctypes.byref(entree), ctypes.byref(bande))
                    action = "connectée" if canal >= 0 else "échec"
                bilan.append((port, n, en_ligne, diffusion, connexions, canal, action))
    finally:
        _raw.raw1394_destroy_handle(poignee)
    return bilan


if __name__ == "__main__":
    import sys
    print(ouvrir_sorties(connecter="--connecter" in sys.argv))
