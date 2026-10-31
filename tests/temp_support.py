"""Retry only transient Windows delete-pending directory errors in fixtures."""
import time


def cleanup_directory(directory):
    for attempt in range(10):
        try:
            directory.cleanup()
            return
        except OSError as error:
            if getattr(error,'winerror',None)!=145 or attempt==9: raise
            time.sleep(.05)
