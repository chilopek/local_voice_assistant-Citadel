"""Short Windows notification sound; no generated audio assets or network."""
import sys


def play_chime():
    if sys.platform=='win32':
        import winsound
        winsound.PlaySound('SystemAsterisk',winsound.SND_ALIAS|winsound.SND_ASYNC|winsound.SND_NODEFAULT)
